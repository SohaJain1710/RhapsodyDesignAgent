'use strict';

const vscode = require('vscode');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const express = require('express');

// ── Paths ────────────────────────────────────────────────────────────────
// ROOT_DIR auto-configured by setup.ps1 — fallback uses extension location
const ROOT_DIR = (() => {
    const p = require('path');
    // config.json written by setup.ps1 contains the actual path
    const cfgFile = p.join(__dirname, '..', 'config.json');
    try {
        return JSON.parse(require('fs').readFileSync(cfgFile, 'utf8')).tools_path
               ? p.dirname(JSON.parse(require('fs').readFileSync(cfgFile, 'utf8')).tools_path)
               : p.join(__dirname, '..');
    } catch { return p.join(__dirname, '..'); }
})();
const PYTHON = path.join(ROOT_DIR, '.venv', 'Scripts', 'python.exe');
// Write-heavy runtime files live OUTSIDE OneDrive to avoid sync-lock conflicts
// with actively-written files (SQLite, temp JSON).
const RUNTIME_DIR = (() => {
    const cfgFile = require('path').join(__dirname, '..', 'config.json');
    try { return JSON.parse(require('fs').readFileSync(cfgFile, 'utf8')).runtime_dir || 'C:\\RhapsodyAIAgent_runtime'; }
    catch { return 'C:\\RhapsodyAIAgent_runtime'; }
})();
if (!fs.existsSync(RUNTIME_DIR)) { fs.mkdirSync(RUNTIME_DIR, { recursive: true }); }

const UNIFIED_GRAPH = path.join(ROOT_DIR, 'tools', 'design_graph_unified.py');

// ── LM Proxy ─────────────────────────────────────────────────────────────
// The ONLY place model.sendRequest()-equivalent access happens now. Python's
// ChatOpenAI client (base_url=http://localhost:3000/v1) calls this endpoint
// for every LLM decision the graph needs — use-case matching, patch
// proposals, new-diagram design, blueprint generation. extension.js makes
// zero model calls itself and holds no prompt logic; it's pure translation.
let lmProxyServer = null;
let outputChannel = null;
let pendingSession = null;

function startLmProxy(outputChannel) {
    const app = express();
    app.use(express.json({ limit: '25mb' }));

    app.post('/v1/chat/completions', async (req, res) => {
        try {
            const { messages, model: requestedModel } = req.body;

            const [model] = await vscode.lm.selectChatModels({
                vendor: 'copilot',
                family: requestedModel && requestedModel !== 'gpt-4' ? requestedModel : undefined,
            });
            if (!model) {
                return res.status(503).json({ error: { message: 'No language model available. Is Copilot signed in?' } });
            }

            const vscodeMessages = messages.map(m =>
                m.role === 'assistant'
                    ? vscode.LanguageModelChatMessage.Assistant(m.content)
                    : vscode.LanguageModelChatMessage.User(m.content)
            );

            const cts = new vscode.CancellationTokenSource();
            const response = await model.sendRequest(vscodeMessages, {}, cts.token);

            let fullText = '';
            for await (const fragment of response.text) fullText += fragment;

            res.json({
                id: 'chatcmpl-local',
                object: 'chat.completion',
                model: model.family,
                choices: [{ index: 0, message: { role: 'assistant', content: fullText }, finish_reason: 'stop' }],
                usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
            });
        } catch (err) {
            outputChannel.appendLine(`[lm-proxy] error: ${err.message}`);
            res.status(500).json({ error: { message: err.message } });
        }
    });

    return app.listen(3000, () => outputChannel.appendLine('[lm-proxy] listening on :3000'));
}

// ── Python runner ────────────────────────────────────────────────────────
function runPython(scriptPath, args) {
    return new Promise((resolve, reject) => {
        const proc = spawn(PYTHON, [scriptPath, ...args], {
            cwd: ROOT_DIR,
            env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
        });

        let stdout = '';
        let stderr = '';
        proc.stdout.on('data', d => { stdout += d.toString(); outputChannel && outputChannel.appendLine(d.toString().trimEnd()); });
        proc.stderr.on('data', d => { stderr += d.toString(); outputChannel && outputChannel.appendLine(d.toString().trimEnd()); });

        proc.on('close', code => {
            if (code === 0) resolve({ stdout, stderr });
            else reject(new Error(stderr || stdout));
        });
        proc.on('error', err => reject(new Error(`Failed to start Python: ${err.message}`)));
    });
}

// ── Intent parsing — /design and /design_bp only ────────────────────────
function parseIntent(text, commandName) {
    const compMatch = text.match(/\b(rb_\w+)\b/);
    const reqMatches = text.match(/\bSRS_\w+_\d+\b/g) || [];

    if (commandName === 'design_resume') {
        return { intent: 'design_resume', component: compMatch ? compMatch[1] : '', fullText: text.trim() };
    }

    if (/^\s*(apply|resume)\b/i.test(text.replace(/rb_\w+/g, '').trim())) {
        return { intent: 'design_resume', component: compMatch ? compMatch[1] : '', fullText: text.trim() };
    }

    // Detect requirement source selection (from button click query)
    const sourceMatch = text.match(/\b(rhapsody|excel)\b/i);
    const excelPathMatch = text.match(/"([^"]+\.xlsx?)"/i) || text.match(/([A-Za-z]:[\\\/][^"\s]+\.xlsx?)/i);
    if (sourceMatch && (compMatch || excelPathMatch)) {
        const src = sourceMatch[1].toLowerCase();
        return {
            intent    : 'design_source',
            source    : src,
            excelPath : excelPathMatch ? excelPathMatch[1] : null,
            component : compMatch ? compMatch[1] : '',
            fullText  : text.trim(),
        };
    }

    return {
        intent: 'design',
        component: compMatch ? compMatch[1] : '',
        requirementIds: reqMatches,
        fullText: text.trim(),
    };
}

function threadKeyFor(requirementIds) {
    return [...requirementIds].sort().join(',');
}

function outputFileFor(component, requirementIds) {
    const key = threadKeyFor(requirementIds).replace(/[^\w,]/g, '_');
    return path.join(RUNTIME_DIR, `_design_output_${component}_${key}.json`);
}

function renderRequirementsTable(stream, output) {
    const reqs = output.requirements_considered || [];
    if (!reqs.length) return;
    stream.markdown(`\n### 📋 Requirements Considered (${reqs.length})\n`);
    let tbl = '\n| Requirement | Use Case | Description |\n|---|---|---|\n';
    reqs.forEach(r => {
        const cleanText = (r.text||'').replace(/[\r\n|]+/g,' ').substring(0,80);
        tbl += '| `' + r.id + '` | ' + (r.usecase||'—') + ' | ' + cleanText + ' |\n';
    });
    stream.markdown(tbl + '\n');
}

// Edits/feedback submitted from a review webview, picked up by /design_resume --from-review
function reviewEditsFile(component) {
    return path.join(RUNTIME_DIR, `_review_edits_${component}.json`);
}

function resumeFromWebview(component, resumeValue) {
    fs.writeFileSync(reviewEditsFile(component), JSON.stringify(resumeValue), 'utf8');
    return vscode.commands.executeCommand('workbench.action.chat.open', {
        query: '@rhapsody /design_resume ' + component + ' --from-review',
    });
}

// ── HTML/script escaping for webviews ────────────────────────────────────
function escHtml(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
// JSON that is safe to embed inside an inline <script> (no "</script>" breakout)
function jsonForScript(v) {
    return JSON.stringify(v).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
}

// ── Run (or resume) the unified graph, relaying interrupts as chat ───────
async function runUnifiedGraph(component, requirementIds, requirementTexts, resumeValue, stream, token) {
    const outputFile = outputFileFor(component, requirementIds);
    // Graph reads requirements from Rhapsody or Excel based on source
    const source = requirementTexts && requirementTexts.requirementSource || 'rhapsody';
    const args = ['--component', component, '--source', source, '--output', outputFile];
    if (source === 'excel' && requirementTexts && requirementTexts.excelPath) {
        args.push('--excel', requirementTexts.excelPath);
    }

    if (resumeValue !== undefined) {
        // Pass the resume value through a file: edited diagrams can be large and
        // multi-line, which does not survive the Windows command line reliably.
        const resumeFile = path.join(RUNTIME_DIR, `_resume_${component}.json`);
        fs.writeFileSync(resumeFile, JSON.stringify(resumeValue), 'utf8');
        args.push('--resume-file', resumeFile);
    }
    // A fresh run clears the old checkpoint for this component inside Python
    // (design_checkpoints.db is shared by all components, so it is not deleted here).

    // Never show a stale result from a previous run if Python dies early
    try { fs.unlinkSync(outputFile); } catch {}

    stream.progress('Running design graph...');

    // Start LLM relay BEFORE Python so it can handle requests immediately
    const stopRelay = await runLlmRelay(token);

    try {
        await runPython(UNIFIED_GRAPH, args);
    } catch (e) {
        stopRelay();
        if (!fs.existsSync(outputFile)) {
            stream.markdown(`**Graph failed:** \`${e.message.split('\n')[0]}\``);
            return;
        }
    }

    stopRelay();

    // Show warning if no approved requirements
    const progressFile = path.join(RUNTIME_DIR, `_progress_${component}.json`);
    if (fs.existsSync(progressFile)) {
        const progress = JSON.parse(fs.readFileSync(progressFile, 'utf8'));
        if (progress.step === 'no_approved_requirements') {
            stream.markdown(`\n> ⚠️ **No Approved Requirements Found**\n>\n> ${progress.warning}`);
        }
        try { fs.unlinkSync(progressFile); } catch {}
    }

    const output = JSON.parse(fs.readFileSync(outputFile, 'utf8'));

    if (output.status === 'interrupted') {
        const data = output.data || {};
        const comp = data.component || component;
        pendingSession = { stream, token, component: comp };

        // Report what this run already wrote to Rhapsody before pausing again
        if (output.applied_usecases && output.applied_usecases.length) {
            stream.markdown(`\n✅ **Applied to Rhapsody:** ${output.applied_usecases.join(', ')}`);
        }
        if (output.errors_so_far && output.errors_so_far.length) {
            stream.markdown(`\n**Errors so far:**\n${output.errors_so_far.map(e => `- ${String(e).split('\n')[0]}`).join('\n')}`);
        }

        // ── HITL #1: Analysis AD review (after update_ad_parse, before design elements) ──
        if (data.phase === 'ad_review') {
            stream.markdown(`\n---\n## 🔄 Analysis AD Review — ${data.usecase || comp}`);
            stream.markdown('\nThe Analysis Activity Diagram has been updated based on the requirements. Please review it before the pipeline designs operations, interfaces, and IBD.\n');
            renderRequirementsTable(stream, output);
            if (data.updated_ad && data.updated_ad.trim()) {
                stream.markdown('\n### Updated Analysis Activity Diagram\n```mermaid\n' + data.updated_ad + '\n```');
            }
            const reqMap = data.req_map || {};
            if (Object.keys(reqMap).length) {
                let t = '\n**Requirement links per action**\n\n| Action | Requirements |\n|---|---|\n';
                Object.entries(reqMap).forEach(([a, r]) => { t += '| `' + a + '` | ' + (r || []).join(', ') + ' |\n'; });
                stream.markdown(t);
            }
            stream.markdown(
                '\n---\n**Choose an action:** approve the AD, or open the editor to correct it or ask the LLM to regenerate it with your feedback.' +
                '\n\nYou can also reply `@rhapsody /design_resume ' + comp + ' <your feedback>` to regenerate, ' +
                'or paste a corrected Mermaid `flowchart` after the component name to use it as-is.');
            stream.button({ command: 'rhapsody.design.apply',    title: '✅ Approve AD & Continue', arguments: [comp] });
            stream.button({ command: 'rhapsody.design.reviewAd', title: '✏️ Edit AD / Give Feedback', arguments: [comp, outputFile] });
            return;
        }

        // ── HITL #2: full design review ──
        stream.markdown(`\n---\n## ⏸️ Design Review — ${data.usecase || comp}`);
        renderRequirementsTable(stream, output);

        // Updated AD
        if (data.updated_ad && data.updated_ad.trim()) {
            stream.markdown('\n### 🔄 Updated Analysis Activity Diagram\n```mermaid\n' + data.updated_ad + '\n```');
        }

        // Validation warnings (e.g. realizer was not a module class)
        if (data.warnings && data.warnings.length) {
            stream.markdown('\n> ⚠️ **Please check:**\n' + data.warnings.map(w => '> - ' + w).join('\n') + '\n');
        }
        const isId = v => /^\w+$/.test(v || '');
        const sigOf = op => (op.name || '') + '(' + (op.arguments||[]).map(a => a.name + ': ' + (a.type||'')).join(', ') + ') ' + (op.return_type||'void');

        // New Operations (grouped by owning module)
        if (data.new_operations && data.new_operations.length) {
            stream.markdown(`\n### ⚙️ Proposed New Operations (${data.new_operations.length})`);
            const byModule = {};
            data.new_operations.forEach(op => {
                const mod = op.module || comp;
                (byModule[mod] = byModule[mod] || []).push(op);
                const args = (op.arguments||[]).map(a => a.name + ': ' + (a.type||'')).join(', ');
                stream.markdown(
                    '\n**`' + op.name + '(' + args + ')` → `' + (op.return_type||'void') + '`**' +
                    '\n- Module: `' + mod + '`' +
                    '\n- Visibility: `' + (op.visibility||'public') + '`' +
                    '\n- Rationale: ' + (op.rationale||'') +
                    '\n- Satisfies: `' + (op.req_ids||[]).join(', ') + '`'
                );
            });
            let cd = 'classDiagram\n';
            Object.entries(byModule).forEach(([mod, ops]) => {
                cd += '    class ' + mod.replace(/\W/g, '_') + ' {\n';
                ops.forEach(op => { cd += '        ' + ({ private: '-', protected: '#' }[op.visibility] || '+') + sigOf(op) + '\n'; });
                cd += '    }\n';
            });
            stream.markdown('\n**BDD Change:**\n```mermaid\n' + cd + '```');
        } else {
            stream.markdown('\n### ⚙️ Operations\n> No new operations needed.');
        }

        // New Interfaces + realizations (realizer is always a module class)
        const reals = data.realizations || [];
        if ((data.new_interfaces && data.new_interfaces.length) || reals.length) {
            stream.markdown('\n### 🔌 Proposed New Interfaces (' + (data.new_interfaces||[]).length + ')');
            let cd2 = 'classDiagram\n';
            (data.new_interfaces||[]).forEach(intf => {
                const ops = intf.operations || [];
                stream.markdown(
                    '\n**`' + intf.name + '`**' +
                    '\n- Realized by: `' + (intf.realized_by || '— not set —') + '`' +
                    (ops.length ? '\n- Operations: ' + ops.map(op => '`' + sigOf(op) + '`').join(', ') : '')
                );
                cd2 += '    class ' + intf.name.replace(/\W/g, '_') + ' {\n        <<Interface>>\n';
                ops.forEach(op => { cd2 += '        +' + sigOf(op) + '\n'; });
                cd2 += '    }\n';
            });
            reals.forEach(r => {
                if (isId(r.specific) && isId(r.general)) cd2 += '    ' + r.specific + ' ..|> ' + r.general + ' : realizes\n';
            });
            stream.markdown('\n**BDD Interfaces Change:**\n```mermaid\n' + cd2 + '```');
        }

        // IBD Changes
        const ports = (data.ibd_delta && data.ibd_delta.ports) || [];
        const links = (data.ibd_delta && data.ibd_delta.links) || [];
        if (ports.length || links.length) {
            stream.markdown('\n### 🔗 Proposed IBD Changes');
            ports.forEach(p => {
                stream.markdown(
                    '\n**Port: `' + p.name + '`**' +
                    ((p.provided||[]).length ? '\n- Provides: `' + p.provided.join(', ') + '`' : '') +
                    ((p.required||[]).length ? '\n- Requires: `' + p.required.join(', ') + '`' : '')
                );
            });
            links.forEach(l => {
                stream.markdown('\n**Link:** `' + (l.from||'') + '` → `' + (l.to||'') + '`');
            });
        }

        stream.markdown('\n---\n**Choose an action:**');
        stream.button({ command: 'rhapsody.design.apply',  title: 'Apply & Draw',  arguments: [comp] });
        stream.button({ command: 'rhapsody.design.review', title: 'Review & Edit', arguments: [comp, outputFile] });
        return;
    }

    if (output.status === 'error') {
        stream.markdown(`**Error:** ${output.error}`);
        if (output.traceback) {
            stream.markdown(`\`\`\`\n${output.traceback}\n\`\`\``);
        }
        return;
    }

    stream.markdown(`\n---\n### ${output.success ? '✅' : '⚠️'} ${output.summary || 'Done.'}`);
    if (output.errors && output.errors.length) {
        stream.markdown(`\n**Errors:**\n${output.errors.map(e => `- ${e}`).join('\n')}`);
    }
}


// ── Ask requirement source via VS Code chat buttons ──────────────────────
async function askRequirementSource(stream, component, promptText) {
    // Check if user already specified in prompt — skip UI
    if (/excel/i.test(promptText))    return 'excel';
    if (/rhapsody/i.test(promptText)) return 'rhapsody';

    // Show VS Code chat button UI
    stream.markdown(`### 📋 Requirements Source for **${component}**\n\nWhere should I load requirements from?`);

    stream.button({
        command: 'rhapsody.design.source',
        title: '$(database) From Rhapsody SRS',
        arguments: [component, 'rhapsody'],
    });

    stream.button({
        command: 'rhapsody.design.source',
        title: '$(table) From Excel File',
        arguments: [component, 'excel'],
    });

    return null;  // Wait for button click — handled via command registration
}


// ── LLM relay: polls for Python requests, calls LLM, writes response ─────────
const LLM_REQUEST_FILE  = path.join(RUNTIME_DIR, 'llm_request.json');
const LLM_RESPONSE_FILE = path.join(RUNTIME_DIR, 'llm_response.json');
const LLM_ERROR_FILE    = path.join(RUNTIME_DIR, 'llm_error.json');

async function runLlmRelay(token) {
    // Returns a cleanup function
    let running = true;

    outputChannel && outputChannel.appendLine('[relay] started polling');
    const poll = setInterval(async () => {
        if (!running) return;
        if (!fs.existsSync(LLM_REQUEST_FILE)) return;
        outputChannel && outputChannel.appendLine('[relay] request found, processing...');

        // Read and delete request file atomically
        let request;
        try {
            const raw = fs.readFileSync(LLM_REQUEST_FILE, 'utf8');
            fs.unlinkSync(LLM_REQUEST_FILE);
            request = JSON.parse(raw);
        } catch { return; }

        try {
            const [model] = await vscode.lm.selectChatModels({ vendor: 'copilot' });
            if (!model) throw new Error('No Copilot model available');

            const messages = [];
            if (request.system) {
                messages.push(vscode.LanguageModelChatMessage.User(
                    `[System]: ${request.system}`));
            }
            messages.push(vscode.LanguageModelChatMessage.User(request.prompt));

            // Use the original chat request token for proper auth context
            const cts = new vscode.CancellationTokenSource();
            const response = await model.sendRequest(messages, {}, cts.token);

            let fullText = '';
            for await (const fragment of response.text) fullText += fragment;

            fs.writeFileSync(LLM_RESPONSE_FILE,
                JSON.stringify({ content: fullText }), 'utf8');
        } catch (e) {
            outputChannel && outputChannel.appendLine(`[relay] LLM error: ${e.message}`);
            fs.writeFileSync(LLM_ERROR_FILE,
                JSON.stringify({ error: e.message }), 'utf8');
        }
    }, 300);

    return () => {
        running = false;
        clearInterval(poll);
        // Clean up files
        for (const f of [LLM_REQUEST_FILE, LLM_RESPONSE_FILE, LLM_ERROR_FILE]) {
            try { fs.unlinkSync(f); } catch {}
        }
    };
}

// ── Design Review Webview ─────────────────────────────────────────────────────
function openReviewWebview(context, data, comp, outputFile, stream, token) {
    outputChannel && outputChannel.appendLine(`[review] opening webview for ${comp}, usecase=${data.usecase}, data keys=${Object.keys(data).join(',')}`);
    const panel = vscode.window.createWebviewPanel(
        'rhapsodyDesignReview',
        `Design Review — ${data.usecase || comp}`,
        vscode.ViewColumn.One,
        { enableScripts: true, retainContextWhenHidden: true }
    );

    const safeData = jsonForScript({
        component       : comp,
        usecase         : data.usecase || '',
        updated_ad      : data.updated_ad || '',
        static_view     : data.static_view || '',
        req_map         : data.req_map || {},
        new_operations  : data.new_operations || [],
        new_interfaces  : data.new_interfaces || [],
        realizations    : data.realizations || [],
        modules         : data.modules || [],
        warnings        : data.warnings || [],
        ibd_delta       : data.ibd_delta || { ports: [], links: [] },
    });

    panel.webview.html = getReviewWebviewHtml(safeData, comp);

    // Apply / feedback RESUME the paused graph (HITL #2) so that the approved
    // design is applied by the graph and the remaining use cases continue.
    panel.webview.onDidReceiveMessage(async msg => {
        if (msg.command === 'apply') {
            panel.dispose();
            await resumeFromWebview(comp, { approved: true, edits: msg.data });
        } else if (msg.command === 'feedback') {
            panel.dispose();
            await resumeFromWebview(comp, { approved: false, feedback: msg.feedback || '' });
        } else if (msg.command === 'cancel') {
            panel.dispose();
        }
    }, undefined, context ? context.subscriptions : []);
}

// ── Analysis AD Review Webview (HITL #1) ─────────────────────────────────────
function openAdReviewWebview(context, data, comp, requirements) {
    const panel = vscode.window.createWebviewPanel(
        'rhapsodyAdReview',
        `AD Review — ${data.usecase || comp}`,
        vscode.ViewColumn.One,
        { enableScripts: true, retainContextWhenHidden: true }
    );
    const safeData = jsonForScript({
        component    : comp,
        usecase      : data.usecase || '',
        updated_ad   : data.updated_ad || '',
        req_map      : data.req_map || {},
        requirements : requirements || [],
    });
    panel.webview.html = getAdReviewWebviewHtml(safeData, comp);

    panel.webview.onDidReceiveMessage(async msg => {
        if (msg.command === 'approve') {
            panel.dispose();
            await resumeFromWebview(comp, msg.edited
                ? { approved: true, updated_ad: msg.updated_ad, req_map: msg.req_map }
                : { approved: true });
        } else if (msg.command === 'feedback') {
            panel.dispose();
            await resumeFromWebview(comp, { approved: false, feedback: msg.feedback || '' });
        } else if (msg.command === 'cancel') {
            panel.dispose();
        }
    }, undefined, context ? context.subscriptions : []);
}

function getAdReviewWebviewHtml(safeData, comp) {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Analysis AD Review</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>${REVIEW_CSS}</style>
</head>
<body>
<script>mermaid.initialize({ startOnLoad: false, theme: 'dark' });</script>
<h1>🔄 Analysis AD Review — ${escHtml(comp)}</h1>
<div id="app"></div>
<script>
const vscodeApi = acquireVsCodeApi();
const RAW = ${safeData};

function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls)  e.className = cls;
    if (html) e.innerHTML = html;
    return e;
}
async function renderMermaid(id, code) {
    const target = document.getElementById(id);
    if (!target || !code || !code.trim()) return;
    try {
        const { svg } = await mermaid.render('m' + id + Date.now(), code);
        target.innerHTML = svg;
    } catch(e) {
        target.innerHTML = '<span style="color:#f87171;font-size:11px">⚠ ' + esc(e.message) + '</span>';
    }
}

const app = document.getElementById('app');
app.appendChild(el('div', 'hint', 'Use case: <code>' + esc(RAW.usecase) + '</code>'));

if (RAW.requirements.length) {
    const rDiv = el('div', 'section');
    rDiv.appendChild(el('h2', '', '📋 Requirements Considered (' + RAW.requirements.length + ')'));
    RAW.requirements.forEach(r => {
        rDiv.appendChild(el('div', 'item', '<div class="details"><div class="title"><code>' + esc(r.id) + '</code></div><div class="meta">' + esc(r.text) + '</div></div>'));
    });
    app.appendChild(rDiv);
}

const edDiv = el('div', 'section');
edDiv.appendChild(el('h2', '', '📊 Analysis Activity Diagram'));
const med = el('div', 'med');
med.appendChild(el('label', '', 'Edit the Mermaid flowchart (live preview below)'));
const ta = document.createElement('textarea'); ta.value = RAW.updated_ad; ta.style.height = '260px';
let timer = null;
ta.oninput = () => { clearTimeout(timer); timer = setTimeout(() => renderMermaid('ad-preview', ta.value), 600); };
med.appendChild(ta);
const resetBtn = el('button', 'btn-ref', '↺ Reset to proposal');
resetBtn.onclick = () => { ta.value = RAW.updated_ad; renderMermaid('ad-preview', ta.value); };
const row = el('div', 'ed-actions'); row.appendChild(resetBtn); med.appendChild(row);
const prev = el('div', 'preview'); prev.id = 'ad-preview'; med.appendChild(prev);
edDiv.appendChild(med);

const reqMap = RAW.req_map || {};
if (Object.keys(reqMap).length) {
    edDiv.appendChild(el('h2', '', '🔗 Requirement links per action'));
    Object.entries(reqMap).forEach(([a, r]) => {
        edDiv.appendChild(el('div', 'meta', '<code>' + esc(a) + '</code> → ' + esc((r || []).join(', '))));
    });
}
app.appendChild(edDiv);

const fbDiv = el('div', 'section');
fbDiv.appendChild(el('h2', '', '💬 Feedback for the LLM (optional)'));
const fbTa = document.createElement('textarea'); fbTa.className = 'fb';
fbTa.placeholder = 'Describe what is wrong. "Regenerate with Feedback" asks the LLM to redo the AD using this feedback.';
fbDiv.appendChild(fbTa);
app.appendChild(fbDiv);

const btnRow = el('div', 'btn-row');
const cancelBtn = el('button', 'btn-cancel', '✗ Cancel');
cancelBtn.onclick = () => vscodeApi.postMessage({ command: 'cancel' });
const regenBtn = el('button', 'btn-regen', '🔁 Regenerate with Feedback');
regenBtn.onclick = () => {
    const fb = fbTa.value.trim();
    if (!fb) { fbTa.focus(); fbTa.placeholder = 'Enter feedback first, then click Regenerate.'; return; }
    vscodeApi.postMessage({ command: 'feedback', feedback: fb });
};
const approveBtn = el('button', 'btn-apply', '✅ Approve AD & Continue');
approveBtn.onclick = () => {
    const edited = ta.value.trim() !== (RAW.updated_ad || '').trim();
    vscodeApi.postMessage({ command: 'approve', edited, updated_ad: ta.value, req_map: RAW.req_map || {} });
};
btnRow.appendChild(cancelBtn); btnRow.appendChild(regenBtn); btnRow.appendChild(approveBtn);
app.appendChild(btnRow);

setTimeout(() => renderMermaid('ad-preview', ta.value), 100);
</script>
</body>
</html>`;
}

const REVIEW_CSS = `
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--vscode-font-family, sans-serif); font-size: 13px;
       background: var(--vscode-editor-background, #1e1e1e);
       color: var(--vscode-editor-foreground, #d4d4d4); padding: 16px; }
h1  { font-size: 18px; margin-bottom: 16px; }
h2  { font-size: 13px; margin: 16px 0 8px; color: #4fc1ff; text-transform: uppercase; }
.section { background: var(--vscode-editorWidget-background, #252526);
           border: 1px solid #3c3c3c; border-radius: 4px; padding: 12px; margin-bottom: 12px; }
.item { display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px solid #3c3c3c; }
.item:last-child { border-bottom: none; }
.item input[type=checkbox] { margin-top: 2px; width: 15px; height: 15px; cursor: pointer; }
.details { flex: 1; }
.title { font-weight: bold; margin-bottom: 3px; }
.meta  { font-size: 11px; color: #888; margin-bottom: 5px; }
.fields { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 5px; }
.field  { display: flex; flex-direction: column; gap: 2px; }
.field label { font-size: 10px; color: #888; }
.field input, .field select {
  background: #3c3c3c; color: #d4d4d4; border: 1px solid #555;
  border-radius: 3px; padding: 3px 6px; font-size: 12px; min-width: 120px; }
.arg-list { margin-top: 5px; display: flex; flex-direction: column; gap: 3px; }
.arg-row { display: flex; gap: 6px; align-items: center; font-size: 11px; color: #888; }
.arg-row input { width: 130px; }
.tabs { display: flex; gap: 0; border-bottom: 2px solid #3c3c3c; margin-bottom: 12px; }
.tab-btn { padding: 7px 18px; background: none; border: none; color: #888; font-size: 12px; font-weight: bold; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; border-radius: 4px 4px 0 0; }
.tab-btn.active { color: #4fc1ff; border-bottom-color: #4fc1ff; background: #1e1e1e; }
.tab-btn:hover:not(.active) { color: #d4d4d4; background: #2a2a2a; }
.tab-panel { display: none; }
.tab-panel.active { display: flex; flex-direction: column; gap: 6px; }
.med { display: flex; flex-direction: column; gap: 6px; }
.med label { font-size: 12px; font-weight: bold; color: #4fc1ff; }
.med textarea { width: 100%; height: 200px; resize: vertical;
  background: #1e1e1e; color: #d4d4d4; border: 1px solid #555;
  border-radius: 3px; padding: 8px; font-family: monospace; font-size: 12px; }
.preview { background: #fff; border-radius: 4px; padding: 10px; min-height: 120px; overflow: auto; }
.btn-row { display: flex; gap: 10px; margin-top: 20px; justify-content: flex-end; }
button { padding: 8px 18px; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: bold; }
.btn-apply  { background: #0e7a0d; color: #fff; }
.btn-cancel { background: #3a3a3a; color: #d4d4d4; }
.btn-ref    { background: #1a6fab; color: #fff; padding: 4px 10px; font-size: 11px; }
.badge-new  { background: #14532d; color: #86efac; border-radius: 3px; padding: 1px 5px; font-size: 10px; margin-left: 5px; }
code { background: #264f78; color: #9cdcfe; border-radius: 3px; padding: 1px 4px; font-size: 12px; }
.op-select { margin-bottom: 8px; }
.hint { font-size: 11px; color: #888; }
.section.warn { border-color: #b08800; }
.section.warn .meta { color: #e2c08d; font-size: 12px; margin-bottom: 4px; }
.ed-actions { display: flex; gap: 8px; align-items: center; }
textarea.fb { width: 100%; height: 70px; resize: vertical; background: #1e1e1e; color: #d4d4d4;
  border: 1px solid #555; border-radius: 3px; padding: 8px; font-size: 12px; }
.btn-regen { background: #8a5a00; color: #fff; }
.op-select label { font-size: 12px; color: #888; }
.op-select select { background: #3c3c3c; color: #d4d4d4; border: 1px solid #555; border-radius: 3px; padding: 4px 8px; font-size: 12px; }
`;

function getReviewWebviewHtml(safeData, comp) {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Design Review</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>${REVIEW_CSS}</style>
</head>
<body>
<script>mermaid.initialize({ startOnLoad: false, theme: 'dark' });</script>
<h1>⏸️ Design Review — ${escHtml(comp)}</h1>
<div id="app"></div>
<script>
const vscodeApi = acquireVsCodeApi();
const RAW  = ${safeData};
const comp = ${jsonForScript(comp)};

let S = {
    updated_ad     : RAW.updated_ad || '',
    static_view    : RAW.static_view || '',
    static_view_edited: false,
    active_tab     : 'static',
    op_ads         : {},
    new_operations : RAW.new_operations.map(o => ({ ...o, _sel: true })),
    new_interfaces : RAW.new_interfaces.map(i => ({ ...i, _sel: true })),
    ports          : ((RAW.ibd_delta||{}).ports||[]).map(p => ({ ...p, _sel: true })),
    links          : ((RAW.ibd_delta||{}).links||[]).map(l => ({ ...l, _sel: true })),
    sel_op         : RAW.new_operations.length ? RAW.new_operations[0].name : null,
};

// ── helpers ──────────────────────────────────────────────────────────────────
function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls)  e.className = cls;
    if (html) e.innerHTML = html;
    return e;
}
function inp(val, cb, w) {
    const e = document.createElement('input');
    e.type = 'text'; e.value = val || '';
    if (w) e.style.width = w;
    e.oninput = () => cb(e.value);
    return e;
}
function field(label, el) {
    const d = document.createElement('div'); d.className = 'field';
    const l = document.createElement('label'); l.textContent = label;
    d.appendChild(l); d.appendChild(el); return d;
}
function selEl(opts, cur, cb) {
    const s = document.createElement('select');
    opts.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; if (v === cur) o.selected = true; s.appendChild(o); });
    s.onchange = () => cb(s.value); return s;
}

// ── render ────────────────────────────────────────────────────────────────────
function render() {
    const app = document.getElementById('app');
    app.innerHTML = '';

    // Validation warnings
    if ((RAW.warnings || []).length) {
        const wDiv = el('div', 'section warn');
        wDiv.appendChild(el('h2', '', '⚠️ Please check'));
        RAW.warnings.forEach(w => wDiv.appendChild(el('div', 'meta', esc(w))));
        app.appendChild(wDiv);
    }
    const MODULES = RAW.modules || [];

    // Operations
    const opsDiv = el('div', 'section');
    opsDiv.appendChild(el('h2', '', '⚙️ Operations'));
    if (!S.new_operations.length) opsDiv.appendChild(el('p', '', '<span style="color:#888">No new operations proposed.</span>'));
    S.new_operations.forEach((op, i) => {
        const item = el('div', 'item');
        const cb   = document.createElement('input'); cb.type = 'checkbox'; cb.checked = op._sel;
        cb.onchange = () => { S.new_operations[i]._sel = cb.checked; };
        item.appendChild(cb);
        const det = el('div', 'details');
        det.appendChild(el('div', 'title', '<code>' + esc(op.name) + '</code><span class="badge-new">NEW</span>'));
        det.appendChild(el('div', 'meta', 'Returns: <code>' + esc(op.return_type||'void') + '</code> | ' + esc(op.visibility||'public') + ' | ' + esc((op.req_ids||[]).join(', '))));
        const fields = el('div', 'fields');
        fields.appendChild(field('Name',        inp(op.name,            v => renameOp(i, v), '160px')));
        fields.appendChild(field('Return Type', inp(op.return_type||'void', v => S.new_operations[i].return_type = v, '100px')));
        fields.appendChild(field('Visibility',  selEl(['private','public','protected'], op.visibility||'public', v => S.new_operations[i].visibility = v)));
        if (MODULES.length) {
            if (!MODULES.includes(op.module)) S.new_operations[i].module = MODULES[0];
            fields.appendChild(field('Module', selEl(MODULES, S.new_operations[i].module, v => S.new_operations[i].module = v)));
        }
        det.appendChild(fields);
        if (op.arguments && op.arguments.length) {
            const al = el('div', 'arg-list');
            op.arguments.forEach((arg, ai) => {
                const row = el('div', 'arg-row', 'name:');
                row.appendChild(inp(arg.name, v => S.new_operations[i].arguments[ai].name = v, '120px'));
                const t = el('span', '', ' type:'); row.appendChild(t);
                row.appendChild(inp(arg.type||'', v => S.new_operations[i].arguments[ai].type = v, '120px'));
                al.appendChild(row);
            });
            det.appendChild(al);
        }
        item.appendChild(det);
        opsDiv.appendChild(item);
    });
    app.appendChild(opsDiv);

    // Interfaces
    if (S.new_interfaces.length) {
        const intfDiv = el('div', 'section');
        intfDiv.appendChild(el('h2', '', '🔌 Interfaces'));
        S.new_interfaces.forEach((intf, i) => {
            const item = el('div', 'item');
            const cb   = document.createElement('input'); cb.type = 'checkbox'; cb.checked = intf._sel;
            cb.onchange = () => { S.new_interfaces[i]._sel = cb.checked; };
            item.appendChild(cb);
            const det = el('div', 'details');
            det.appendChild(el('div', 'title', '<code>' + esc(intf.name) + '</code><span class="badge-new">NEW</span>'));
            det.appendChild(el('div', 'meta', 'Realized by: ' + esc(intf.realized_by||'—')));
            const fields = el('div', 'fields');
            fields.appendChild(field('Name',        inp(intf.name,            v => S.new_interfaces[i].name = v, '160px')));
            if (MODULES.length) {
                // Only module classes can realize an interface
                const opts = MODULES.includes(intf.realized_by) ? MODULES : ['', ...MODULES];
                fields.appendChild(field('Realized By (module)', selEl(opts, intf.realized_by || '', v => S.new_interfaces[i].realized_by = v)));
            } else {
                fields.appendChild(field('Realized By', inp(intf.realized_by||'', v => S.new_interfaces[i].realized_by = v, '160px')));
            }
            det.appendChild(fields);
            item.appendChild(det);
            intfDiv.appendChild(item);
        });
        app.appendChild(intfDiv);
    }

    // IBD Ports
    if (S.ports.length) {
        const ibdDiv = el('div', 'section');
        ibdDiv.appendChild(el('h2', '', '🔗 IBD Ports'));
        S.ports.forEach((port, i) => {
            const item = el('div', 'item');
            const cb   = document.createElement('input'); cb.type = 'checkbox'; cb.checked = port._sel;
            cb.onchange = () => { S.ports[i]._sel = cb.checked; };
            item.appendChild(cb);
            const det = el('div', 'details');
            det.appendChild(el('div', 'title', '<code>' + esc(port.name) + '</code>'));
            det.appendChild(el('div', 'meta', 'Provides: ' + esc((port.provided||[]).join(', ')) + ' | Requires: ' + esc((port.required||[]).join(', '))));
            const fields = el('div', 'fields');
            fields.appendChild(field('Port Name', inp(port.name, v => S.ports[i].name = v, '160px')));
            det.appendChild(fields);
            item.appendChild(det);
            ibdDiv.appendChild(item);
        });
        app.appendChild(ibdDiv);
    }

    // Diagram sections (3 tabs)
    const mDiv = el('div', 'section');
    mDiv.appendChild(el('h2', '', '📊 Diagrams'));

    const tabs = el('div', 'tabs');
    const tabDefs = [
        { id: 'static',   label: '🏗️ Static View',   ta: 'static-ta',     prev: 'static-preview'   },
        { id: 'analysis', label: '🔄 Analysis View', ta: 'analysis-ta',   prev: 'analysis-preview' },
        { id: 'dynamic',  label: '⚡ Dynamic View',  ta: 'dynamic-op-ta', prev: 'dynamic-preview'  },
    ];
    const tabBtns = {}, tabPanels = {};
    function showTab(id) {
        S.active_tab = id;
        tabDefs.forEach(t => {
            tabBtns[t.id].className   = 'tab-btn'   + (t.id === id ? ' active' : '');
            tabPanels[t.id].className = 'tab-panel' + (t.id === id ? ' active' : '');
        });
        const t  = tabDefs.find(x => x.id === id);
        const ta = document.getElementById(t.ta);
        if (ta) renderMermaid(t.prev, ta.value);
    }
    tabDefs.forEach(t => {
        const btn = el('button', 'tab-btn', t.label);
        btn.onclick = () => showTab(t.id);
        tabBtns[t.id] = btn;
        tabs.appendChild(btn);
    });
    mDiv.appendChild(tabs);

    // ── Static View panel ─────────────────────────────────────────────────
    const staticPanel = el('div', 'tab-panel'); staticPanel.id = 'tab-static';
    tabPanels['static'] = staticPanel;
    staticPanel.appendChild(el('div', 'hint',
        'Generated from the operations and interfaces selected above. If you edit it here, the edited diagram decides which operations, interfaces and realizations are applied.'));
    const st = mermaidEditor(staticPanel, 'Class / Block definition diagram (Mermaid classDiagram)',
        'static-ta', 'static-preview', S.static_view,
        v => { S.static_view = v; S.static_view_edited = true; updateStaticBadge(); });
    const stReset = el('button', 'btn-ref', '↺ Reset to proposal');
    stReset.onclick = () => { S.static_view_edited = false; refreshStatic(true); };
    st.row.appendChild(stReset);
    const stBadge = el('span', 'hint'); stBadge.id = 'static-badge';
    st.row.appendChild(stBadge);
    mDiv.appendChild(staticPanel);

    // ── Analysis View panel ───────────────────────────────────────────────
    const analysisPanel = el('div', 'tab-panel'); analysisPanel.id = 'tab-analysis';
    tabPanels['analysis'] = analysisPanel;
    mermaidEditor(analysisPanel, 'Analysis Activity Diagram', 'analysis-ta', 'analysis-preview',
        S.updated_ad, v => { S.updated_ad = v; });
    mDiv.appendChild(analysisPanel);

    // ── Dynamic View panel ────────────────────────────────────────────────
    const dynamicPanel = el('div', 'tab-panel'); dynamicPanel.id = 'tab-dynamic';
    tabPanels['dynamic'] = dynamicPanel;
    if (S.new_operations.length) {
        const opSel = el('div', 'op-select');
        opSel.appendChild(el('label', '', 'Operation: '));
        const opNames = S.new_operations.map(o => o.name);
        const opSelEl = selEl(opNames, S.sel_op || opNames[0], v => {
            S.sel_op = v;
            const ta = document.getElementById('dynamic-op-ta');
            ta.value = S.op_ads[v] || defaultOpAD(S.new_operations.find(o => o.name === v));
            renderMermaid('dynamic-preview', ta.value);
        });
        opSelEl.id = 'op-sel';
        opSel.appendChild(opSelEl);
        dynamicPanel.appendChild(opSel);
        dynamicPanel.appendChild(el('div', 'hint',
            'Only operations whose diagram you edit here are created from this editor; the others are generated by the LLM after Apply.'));
        const curOp = S.new_operations.find(o => o.name === S.sel_op) || S.new_operations[0];
        mermaidEditor(dynamicPanel, 'Operation Activity Diagram', 'dynamic-op-ta', 'dynamic-preview',
            S.op_ads[S.sel_op] || defaultOpAD(curOp),
            v => { if (S.sel_op) S.op_ads[S.sel_op] = v; });
    } else {
        dynamicPanel.appendChild(el('p', 'hint',
            'No new operations are proposed, so there is no operation activity diagram to edit.'));
    }
    mDiv.appendChild(dynamicPanel);

    app.appendChild(mDiv);

    // Feedback
    const fbDiv = el('div', 'section');
    fbDiv.appendChild(el('h2', '', '💬 Feedback (optional)'));
    const fbTa = document.createElement('textarea');
    fbTa.id = 'feedback-ta'; fbTa.className = 'fb';
    fbTa.placeholder = 'Describe what should change. "Regenerate with Feedback" sends this to the LLM to redesign the operations and interfaces.';
    fbDiv.appendChild(fbTa);
    app.appendChild(fbDiv);

    // Buttons
    const btnRow = el('div', 'btn-row');
    const cancelBtn = el('button', 'btn-cancel', '✗ Cancel');
    cancelBtn.onclick = () => vscodeApi.postMessage({ command: 'cancel' });
    const regenBtn = el('button', 'btn-regen', '🔁 Regenerate with Feedback');
    regenBtn.onclick = () => {
        const fb = fbTa.value.trim();
        if (!fb) { fbTa.focus(); fbTa.placeholder = 'Enter feedback first, then click Regenerate.'; return; }
        vscodeApi.postMessage({ command: 'feedback', feedback: fb });
    };
    const applyBtn = el('button', 'btn-apply', '✅ Apply to Rhapsody');
    applyBtn.onclick = applyChanges;
    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(regenBtn);
    btnRow.appendChild(applyBtn);
    app.appendChild(btnRow);

    // Keep the Static View in sync with the form (checkboxes, names, types…)
    const onFormChange = e => {
        if (e.target.closest('.tab-panel') || e.target.id === 'feedback-ta') return;
        refreshStatic(false);
    };
    app.addEventListener('input',  onFormChange);
    app.addEventListener('change', onFormChange);

    refreshStatic(false);
    showTab(S.active_tab || 'static');
}

// Renaming an operation keeps its edited Dynamic View diagram and the selector in sync
function renameOp(i, newName) {
    const oldName = S.new_operations[i].name;
    S.new_operations[i].name = newName;
    if (S.op_ads[oldName] !== undefined) { S.op_ads[newName] = S.op_ads[oldName]; delete S.op_ads[oldName]; }
    if (S.sel_op === oldName) S.sel_op = newName;
    const sel = document.getElementById('op-sel');
    if (sel && sel.options[i]) { sel.options[i].value = newName; sel.options[i].textContent = newName; }
}

// Mermaid textarea + debounced live preview + "Preview" button
function mermaidEditor(panel, labelText, taId, prevId, value, onChange) {
    const ed = el('div', 'med');
    ed.appendChild(el('label', '', esc(labelText)));
    const ta = document.createElement('textarea');
    ta.id = taId; ta.value = value || '';
    let timer = null;
    ta.oninput = () => {
        onChange(ta.value);
        clearTimeout(timer);
        timer = setTimeout(() => renderMermaid(prevId, ta.value), 600);
    };
    ed.appendChild(ta);
    const row = el('div', 'ed-actions');
    const ref = el('button', 'btn-ref', '↻ Preview');
    ref.onclick = () => renderMermaid(prevId, ta.value);
    row.appendChild(ref);
    ed.appendChild(row);
    const prev = el('div', 'preview'); prev.id = prevId;
    ed.appendChild(prev);
    panel.appendChild(ed);
    return { ta, row };
}

function mId(name) { return String(name || '').replace(/\\W/g, '_'); }

function buildStaticView() {
    const visSym = v => v === 'public' ? '+' : v === 'protected' ? '#' : '-';
    const sig = op => mId(op.name) + '(' + (op.arguments||[]).map(a => a.name + ': ' + (a.type||'')).join(', ') + ') ' + (op.return_type||'void');
    const byModule = {};
    S.new_operations.filter(o => o._sel).forEach(op => {
        const mod = op.module || comp;
        (byModule[mod] = byModule[mod] || []).push(op);
    });
    if (!Object.keys(byModule).length) byModule[(RAW.modules || [])[0] || comp] = [];
    let cd = 'classDiagram\\n';
    Object.entries(byModule).forEach(([mod, ops]) => {
        cd += '    class ' + mId(mod) + ' {\\n';
        ops.forEach(op => { cd += '        ' + visSym(op.visibility || 'public') + sig(op) + '\\n'; });
        cd += '    }\\n';
    });
    const intfNames = new Set();
    S.new_interfaces.filter(i => i._sel).forEach(intf => {
        intfNames.add(intf.name);
        cd += '    class ' + mId(intf.name) + ' {\\n        <<Interface>>\\n';
        (intf.operations||[]).forEach(op => { cd += '        +' + sig(op) + '\\n'; });
        cd += '    }\\n';
        if (intf.realized_by && /^\\w+$/.test(intf.realized_by)) {
            cd += '    ' + intf.realized_by + ' ..|> ' + mId(intf.name) + ' : realizes\\n';
        }
    });
    // Realizations of EXISTING interfaces proposed by the LLM
    (RAW.realizations || []).forEach(r => {
        if (!intfNames.has(r.general) && /^\\w+$/.test(r.specific || '') && /^\\w+$/.test(r.general || '')) {
            cd += '    ' + r.specific + ' ..|> ' + r.general + ' : realizes\\n';
        }
    });
    return cd;
}

let staticTimer = null;
function refreshStatic(force) {
    if (S.static_view_edited && !force) { updateStaticBadge(); return; }
    S.static_view = buildStaticView();
    const ta = document.getElementById('static-ta');
    if (ta) ta.value = S.static_view;
    updateStaticBadge();
    if (S.active_tab === 'static') {
        clearTimeout(staticTimer);
        staticTimer = setTimeout(() => renderMermaid('static-preview', S.static_view), 300);
    }
}

function updateStaticBadge() {
    const b = document.getElementById('static-badge');
    if (b) b.textContent = S.static_view_edited
        ? '✏️ Edited: this diagram will be used when you click Apply'
        : 'Auto-generated from the selection above';
}

function defaultOpAD(op) {
    if (!op) return 'flowchart TD\\n    Start([Start]) --> End([End])';
    return 'flowchart TD\\n    Start([Start])\\n    Validate[Validate inputs]\\n    Execute[Execute ' + op.name + ']\\n    Return[Return result]\\n    End([End])\\n    Start --> Validate\\n    Validate --> Execute\\n    Execute --> Return\\n    Return --> End';
}

async function renderMermaid(id, code) {
    const target = document.getElementById(id);
    if (!target || !code || !code.trim()) return;
    try {
        const { svg } = await mermaid.render('m' + id + Date.now(), code);
        target.innerHTML = svg;
    } catch(e) {
        target.innerHTML = '<span style="color:#f87171;font-size:11px">⚠ ' + esc(e.message) + '</span>';
    }
}

function applyChanges() {
    const strip = x => { const {_sel, ...r} = x; return r; };
    vscodeApi.postMessage({ command: 'apply', data: {
        component          : comp,
        usecase            : RAW.usecase,
        updated_ad         : S.updated_ad,
        static_view        : S.static_view,
        static_view_edited : S.static_view_edited,
        req_map            : RAW.req_map || {},
        op_ads             : S.op_ads,
        new_operations     : S.new_operations.filter(o => o._sel).map(strip),
        new_interfaces     : S.new_interfaces.filter(i => i._sel).map(strip),
        realizations       : RAW.realizations || [],
        modules            : RAW.modules || [],
        ibd_delta          : {
            ports: S.ports.filter(p => p._sel).map(strip),
            links: S.links.filter(l => l._sel).map(strip),
        },
    }});
}

render();
</script>
</body>
</html>`;
}

async function handleRhapsodyChat(request, context, stream, token) {
    const commandName = request.command || '';
    outputChannel && outputChannel.appendLine('[debug] command=' + commandName + ' prompt=' + request.prompt.substring(0, 50));
    const intent = parseIntent(request.prompt, commandName);
    outputChannel && outputChannel.appendLine(`[debug] intent='${intent.intent}' component='${intent.component}'`);

    if (!intent.component) {
        stream.markdown('Please include a component name (e.g. `rb_sdm_SafeDataMgt`).');
        return;
    }

    if (intent.intent === 'design_resume') {
        let resumeValue;
        if (/--from-review\b/.test(intent.fullText)) {
            // Edits / feedback submitted from a review webview
            const f = reviewEditsFile(intent.component);
            try {
                resumeValue = JSON.parse(fs.readFileSync(f, 'utf8'));
                fs.unlinkSync(f);
            } catch (e) {
                stream.markdown(`**Could not read the review edits** (\`${e.message}\`). Open the review again and resubmit.`);
                return;
            }
            stream.markdown(resumeValue.approved
                ? 'Continuing with your approved changes...'
                : 'Regenerating with your feedback...');
        } else {
            // Remove only the FIRST component name, so names inside pasted Mermaid survive
            resumeValue = intent.fullText.replace(/\brb_\w+\b/, '').replace(/^\s*(apply|resume)\b\s*/i, '').trim() || 'apply';
        }
        await runUnifiedGraph(intent.component, [], {}, resumeValue, stream, token);
        return;
    }

    // User selected requirement source via button
    if (intent.intent === 'design_source') {
        const label = intent.source === 'excel'
            ? `Excel: \`${intent.excelPath || 'unknown'}\``
            : 'Rhapsody SRS';
        stream.markdown(`Loading requirements from **${label}**...`);
        await runUnifiedGraph(
            intent.component, [],
            { requirementSource: intent.source, excelPath: intent.excelPath },
            undefined, stream, token
        );
        return;
    }

    // Ask user where requirements come from
    const source = await askRequirementSource(stream, intent.component, request.prompt);
    if (!source) return;  // user cancelled or unclear

    await runUnifiedGraph(intent.component, [], { requirementSource: source }, undefined, stream, token);
}

// ── Extension activation ─────────────────────────────────────────────────
// NOTE: register "rhapsody.design.source" in package.json commands array
function activate(context) {
    outputChannel = vscode.window.createOutputChannel('Rhapsody DD Generator');
    outputChannel.appendLine('[Rhapsody] Extension activating...');

    lmProxyServer = startLmProxy(outputChannel);

    const participant = vscode.chat.createChatParticipant('rhapsody.ddgen', handleRhapsodyChat);
    participant.iconPath = new vscode.ThemeIcon('circuit-board');

    context.subscriptions.push(participant);
    context.subscriptions.push(outputChannel);

    // Register button command for requirement source selection
    const sourceCmd = vscode.commands.registerCommand(
        'rhapsody.design.source',
        async (component, source) => {
            let query = `@rhapsody /design ${component} ${source}`;

            if (source === 'excel') {
                // Show native file picker for Excel file
                const uris = await vscode.window.showOpenDialog({
                    title: 'Select Requirements Excel File',
                    canSelectMany: false,
                    filters: { 'Excel Files': ['xlsx', 'xls', 'xlsm'] },
                    defaultUri: vscode.Uri.file(RUNTIME_DIR),
                });
                if (!uris || uris.length === 0) return; // user cancelled
                const excelPath = uris[0].fsPath;
                query = `@rhapsody /design ${component} excel "${excelPath}"`;
            }

            await vscode.commands.executeCommand('workbench.action.chat.open', {
                query,
            });
        }
    );
    context.subscriptions.push(sourceCmd);

    const applyCmd = vscode.commands.registerCommand('rhapsody.design.apply', async (component) => {
        await vscode.commands.executeCommand('workbench.action.chat.open', { query: '@rhapsody /design_resume ' + component + ' apply' });
    });
    context.subscriptions.push(applyCmd);


    const reviewCmd = vscode.commands.registerCommand('rhapsody.design.review',
        async (component, outputFile) => {
            if (!outputFile || !fs.existsSync(outputFile)) {
                vscode.window.showErrorMessage('Design output not found. Run @rhapsody /design first.');
                return;
            }
            const output = JSON.parse(fs.readFileSync(outputFile, 'utf8'));
            const data   = output.data || {};
            openReviewWebview(context, data, component, outputFile,
                pendingSession && pendingSession.stream,
                pendingSession && pendingSession.token);
        }
    );
    context.subscriptions.push(reviewCmd);

    const reviewAdCmd = vscode.commands.registerCommand('rhapsody.design.reviewAd',
        async (component, outputFile) => {
            if (!outputFile || !fs.existsSync(outputFile)) {
                vscode.window.showErrorMessage('AD review data not found. Run @rhapsody /design first.');
                return;
            }
            const output = JSON.parse(fs.readFileSync(outputFile, 'utf8'));
            if (output.status !== 'interrupted' || (output.data || {}).phase !== 'ad_review') {
                vscode.window.showWarningMessage('This Analysis AD review is no longer pending.');
                return;
            }
            openAdReviewWebview(context, output.data || {}, component, output.requirements_considered || []);
        }
    );
    context.subscriptions.push(reviewAdCmd);

    outputChannel.appendLine('[Rhapsody] Extension activated (LM proxy on :3000)');
}

function deactivate() {
    if (lmProxyServer) lmProxyServer.close();
}

module.exports = { activate, deactivate };