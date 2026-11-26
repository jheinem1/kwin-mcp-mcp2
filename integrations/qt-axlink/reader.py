import json, os, sys
import dbus
import gi
gi.require_version("Atspi","2.0")
from gi.repository import Atspi
pid=int(sys.argv[1]); path="/org/a11y/atspi/accessible/"+str(int(sys.argv[2]))
bus=dbus.bus.BusConnection(os.environ['AT_SPI_BUS_ADDRESS'])
daemon=dbus.Interface(bus.get_object('org.freedesktop.DBus','/org/freedesktop/DBus'),'org.freedesktop.DBus')
desktop=Atspi.get_desktop(0); matches=[]
def owned_markers(root):
    todo=[(root,0)]; seen=0; out=set()
    while todo and seen<220:
        w,depth=todo.pop(0)
        if w is None:continue
        seen+=1
        n=w.get_name() or ''
        for t in ('owned-dolphin-alpha.txt','owned-dolphin-bravo.txt'):
            if t in n:out.add(t)
        if depth<9:
            todo.extend((w.get_child_at_index(i),depth+1) for i in range(min(w.get_child_count(),18)))
    return sorted(out)
if desktop is not None and 0<=desktop.get_child_count()<=256:
    for i in range(desktop.get_child_count()):
        app=desktop.get_child_at_index(i)
        if app is None or app.get_process_id()!=pid:continue
        if not 0<=app.get_child_count()<=256:continue
        for j in range(app.get_child_count()):
            root=app.get_child_at_index(j)
            if root is None or root.path!=path or root.get_process_id()!=pid:continue
            owner=root.app.bus_name
            if not owner.startswith(':'):continue
            try:
                if int(daemon.GetConnectionUnixProcessID(owner,timeout=2))!=pid:continue
            except dbus.DBusException:continue
            matches.append({'bus':owner,'path':path,'token':root.get_accessible_id(),'role':root.get_role_name(),'markers':owned_markers(root),'showing':bool(root.get_state_set().contains(Atspi.StateType.SHOWING))})
print(json.dumps(matches[0] if len(matches)==1 else None))
