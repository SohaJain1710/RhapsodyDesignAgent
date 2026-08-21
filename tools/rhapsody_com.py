"""
Shared Rhapsody COM helpers — used by all tools.
"""
import win32com.client
import time
import sys
def get_rhapsody():
    rhapsody = win32com.client.Dispatch("Rhapsody2.Application")
    return rhapsody, rhapsody.projects.Item(1)


def find_package_by_name(parent, name):
    pkgs = parent.packages
    for i in range(1, pkgs.Count + 1):
        pkg = pkgs.Item(i)
        if pkg.name == name:
            return pkg
    return None



def find_package_recursive(parent, name):
    pkgs = parent.packages
    for i in range(1, pkgs.Count + 1):
        pkg = pkgs.Item(i)
        if pkg.name == name:
            return pkg
        r = find_package_recursive(pkg, name)
        if r:
            return r
    return None


def get_class(pkg, name):
    if not pkg:
        return None
    for i in range(1, pkg.classes.Count + 1):
        cls = pkg.classes.Item(i)
        if cls.name == name:
            return cls
    return None


def find_sd_by_name(pkg, name):
    for i in range(1, pkg.sequenceDiagrams.Count + 1):
        sd = pkg.sequenceDiagrams.Item(i)
        if sd.name == name:
            return sd
    return None


def find_class_recursive(parent, name):
    """Find a class by name anywhere under `parent` (packages searched recursively)."""
    if not parent:
        return None
    try:
        for i in range(1, parent.classes.Count + 1):
            cls = parent.classes.Item(i)
            if cls.name == name:
                return cls
    except Exception:
        pass
    try:
        for i in range(1, parent.packages.Count + 1):
            found = find_class_recursive(parent.packages.Item(i), name)
            if found:
                return found
    except Exception:
        pass
    return None


def find_operation(cls, name):
    if not cls:
        return None
    try:
        for i in range(1, cls.operations.Count + 1):
            op = cls.operations.Item(i)
            if op.name == name:
                return op
    except Exception:
        pass
    return None


def find_attribute(cls, name):
    if not cls:
        return None
    try:
        for i in range(1, cls.attributes.Count + 1):
            at = cls.attributes.Item(i)
            if at.name == name:
                return at
    except Exception:
        pass
    return None


def find_stereotype_by_name(root, stereo_name):
    """Find a stereotype COM object by name by sampling existing classes."""
    if not stereo_name:
        return None

    def walk(pkg):
        try:
            for i in range(1, pkg.classes.Count + 1):
                cls = pkg.classes.Item(i)
                try:
                    st = cls.stereotype
                    if st and st.name == stereo_name:
                        return st
                except Exception:
                    pass
        except Exception:
            pass
        try:
            for i in range(1, pkg.packages.Count + 1):
                found = walk(pkg.packages.Item(i))
                if found:
                    return found
        except Exception:
            pass
        return None

    return walk(root)


def find_interface_op(from_class, to_class, op_name):
    """
    Find operation by searching:
    1. Required interfaces on from_class (sender calling out)
    2. Provided interfaces on to_class   (receiver being called)
    """
    # Required interfaces on sender
    try:
        for i in range(1, from_class.ports.Count + 1):
            port = from_class.ports.Item(i)
            try:
                for j in range(1, port.requiredInterfaces.Count + 1):
                    iface = port.requiredInterfaces.Item(j)
                    try:
                        for k in range(1, iface.operations.Count + 1):
                            op = iface.operations.Item(k)
                            if op.name == op_name:
                                return op
                    except:
                        pass
            except:
                pass
    except:
        pass

    # Provided interfaces on receiver
    if to_class:
        try:
            for i in range(1, to_class.ports.Count + 1):
                port = to_class.ports.Item(i)
                try:
                    for j in range(1, port.providedInterfaces.Count + 1):
                        iface = port.providedInterfaces.Item(j)
                        try:
                            for k in range(1, iface.operations.Count + 1):
                                op = iface.operations.Item(k)
                                if op.name == op_name:
                                    return op
                        except:
                            pass
                except:
                    pass
        except:
            pass

    return None


def set_geom(gel, x, y, w, h):
    if not gel:
        return
    try:
        gel.setGraphicalProperty("x",      str(int(x)))
        gel.setGraphicalProperty("y",      str(int(y)))
        gel.setGraphicalProperty("Width",  str(int(w)))
        gel.setGraphicalProperty("Height", str(int(h)))
    except:
        pass


def ll_center_x(idx,
                start=60, width=140, spacing=160):
    return start + idx * (width + spacing) + width // 2

def get_sw_model(project):
    pkgs = project.packages
    for i in range(1, pkgs.Count + 1):
        p = pkgs.Item(i)
        if p.name == "SwModel":
            return p
    return None


def find_element_by_guid(project, guid):
    """
    Locate any model element by its GUID.

    Tries the native project lookup first (fast); falls back to a recursive
    walk over packages/classes/operations/attributes/interfaces if the native
    call is unavailable. Returns the COM element or None.
    """
    if not guid:
        return None
    guid = str(guid).strip("{}").upper()

    # 1. Native lookup (available on most Rhapsody builds)
    for finder in ("findElementByGUID", "getElementByGUID"):
        try:
            fn = getattr(project, finder)
            el = fn(guid)
            if el:
                return el
        except Exception:
            pass

    # 2. Recursive fallback
    def norm(g):
        try:
            return str(g).strip("{}").upper()
        except Exception:
            return ""

    def walk_pkg(pkg):
        # classes (and their operations / attributes)
        try:
            for i in range(1, pkg.classes.Count + 1):
                cls = pkg.classes.Item(i)
                if norm(cls.GUID) == guid:
                    return cls
                try:
                    for j in range(1, cls.operations.Count + 1):
                        op = cls.operations.Item(j)
                        if norm(op.GUID) == guid:
                            return op
                except Exception:
                    pass
                try:
                    for j in range(1, cls.attributes.Count + 1):
                        at = cls.attributes.Item(j)
                        if norm(at.GUID) == guid:
                            return at
                except Exception:
                    pass
        except Exception:
            pass
        # nested packages
        try:
            for i in range(1, pkg.packages.Count + 1):
                found = walk_pkg(pkg.packages.Item(i))
                if found:
                    return found
        except Exception:
            pass
        return None

    try:
        for i in range(1, project.packages.Count + 1):
            found = walk_pkg(project.packages.Item(i))
            if found:
                return found
    except Exception:
        pass
    return None

