'use strict';

const vscode = require('vscode');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const express = require('express');

// ── Paths ────────────────────────────────────────────────────────────────
const ROOT_DIR = 'C:\\Users\\jio2kor\\OneDrive - Bosch Group\\RhapsodyAIAgent';
const PYTHON = path.join(ROOT_DIR, '.venv', 'Scripts', 'python.exe');
// Write-heavy runtime files live OUTSIDE OneDrive to avoid sync-lock conflicts
// with actively-written files (SQLite, temp JSON).
const RUNTIME_DIR = 'C:\\RhapsodyAIAgent_runtime';
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
        args.push('--resume', typeof resumeValue === 'string' ? resumeValue : JSON.stringify(resumeValue));
    }

    // Delete old checkpoint for fresh run (not resume)
    if (resumeValue === undefined) {
        const ckptFile = path.join(RUNTIME_DIR, 'design_checkpoints.db');
        try { fs.unlinkSync(ckptFile); } catch {}
    }

    if (resumeValue === undefined) {
        try { fs.unlinkSync(path.join(RUNTIME_DIR, 'design_checkpoints.db')); } catch {}
    }

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

        stream.markdown(`\n---\n## ⏸️ Design Review — ${data.usecase || comp}`);

        // Requirements considered
        if (output.requirements_considered && output.requirements_considered.length) {
            stream.markdown(`\n### 📋 Requirements Considered (${output.requirements_considered.length})\n`);
            let tbl = '\n| Requirement | Use Case | Description |\n|---|---|---|\n';
            output.requirements_considered.forEach(r => {
                const cleanText = (r.text||'').replace(/[\r\n]+/g,' ').substring(0,80);
                tbl += '| `' + r.id + '` | ' + (r.usecase||'—') + ' | ' + cleanText + ' |\n';
            });
            stream.markdown(tbl + '\n');
        }

        // Updated AD
        if (data.updated_ad && data.updated_ad.trim()) {
            stream.markdown('\n### 🔄 Updated Analysis Activity Diagram\n```mermaid\n' + data.updated_ad + '\n```');
        }

        // New Operations
        if (data.new_operations && data.new_operations.length) {
            stream.markdown(`\n### ⚙️ Proposed New Operations (${data.new_operations.length})`);
            data.new_operations.forEach(op => {
                const args = (op.arguments||[]).map(a => a.name + ': ' + a.type).join(', ');
                const reqIds = (op.req_ids||[]).join(', ');
                stream.markdown(
                    '\n**`' + op.name + '(' + args + ')` → `' + (op.return_type||'void') + '`**' +
                    '\n- Module: `' + comp + '`' +
                    '\n- Visibility: `' + (op.visibility||'private') + '`' +
                    '\n- Rationale: ' + (op.rationale||'') +
                    '\n- Satisfies: `' + reqIds + '`'
                );
            });
            // BDD classDiagram
            let cd = 'classDiagram\n    class ' + comp + ' {\n';
            data.new_operations.forEach(op => {
                const args = (op.arguments||[]).map(a => a.name + ': ' + a.type).join(', ');
                const vis = op.visibility === 'private' ? '-' : '+';
                cd += '        ' + vis + op.name + '(' + args + ') ' + (op.return_type||'void') + '\n';
            });
            cd += '    }';
            stream.markdown('\n**BDD Change:**\n```mermaid\n' + cd + '\n```');
        } else {
            stream.markdown('\n### ⚙️ Operations\n> No new operations needed.');
        }

        // New Interfaces
        if (data.new_interfaces && data.new_interfaces.length) {
            stream.markdown('\n### 🔌 Proposed New Interfaces (' + data.new_interfaces.length + ')');
            let cd2 = 'classDiagram\n';
            data.new_interfaces.forEach(intf => {
                cd2 += '    class ' + intf.name + ' {\n        <<Interface>>\n';
                (intf.operations||[]).forEach(op => {
                    const args = (op.arguments||[]).map(a => a.name + ': ' + a.type).join(', ');
                    cd2 += '        +' + op.name + '(' + args + ') ' + (op.return_type||'void') + '\n';
                    stream.markdown(
                        '\n**`' + intf.name + '`**' +
                        '\n- Realized by: `' + (intf.realized_by||'—') + '`' +
                        '\n- Operation: `' + op.name + '(' + args + ')` → `' + (op.return_type||'void') + '`'
                    );
                });
                cd2 += '    }\n';
                if (intf.realized_by) cd2 += '    ' + intf.realized_by + ' ..|> ' + intf.name + ' : realizes\n';
            });
            stream.markdown('\n**BDD Interfaces Change:**\n```mermaid\n' + cd2 + '\n```');
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
        stream.button({ command: 'rhapsody.design.apply',    title: '✅ Apply & Draw', arguments: [comp] });
        stream.button({ command: 'rhapsody.design.feedback', title: '💬 Provide Feedback', arguments: [comp] });
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

// ── Chat participant ─────────────────────────────────────────────────────
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
        const resumeText = intent.fullText.replace(/\brb_\w+\b/g, '').replace(/^\s*(apply|resume)\b\s*/i, '').trim() || 'apply';
        await runUnifiedGraph(intent.component, [], {}, resumeText, stream, token);
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

    const feedbackCmd = vscode.commands.registerCommand('rhapsody.design.feedback', async (component) => {
        const feedback = await vscode.window.showInputBox({ prompt: 'Enter your feedback', placeHolder: 'e.g. The operation name should follow rb_sdm_ convention...' });
        if (!feedback) return;
        await vscode.commands.executeCommand('workbench.action.chat.open', { query: '@rhapsody /design_resume ' + component + ' ' + feedback });
    });
    context.subscriptions.push(feedbackCmd);

    outputChannel.appendLine('[Rhapsody] Extension activated (LM proxy on :3000)');
}

function deactivate() {
    if (lmProxyServer) lmProxyServer.close();
}

module.exports = { activate, deactivate };