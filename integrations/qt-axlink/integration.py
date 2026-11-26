import collections
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import dbus

b=pathlib.Path(os.environ["AX_WORK"])
s=pathlib.Path(os.environ["AX_STAGE"])
mode=sys.argv[1]
test_lock = mode in {'lock','semantic-lock'}
semantic = mode in {'semantic','semantic-lock'}
no_publisher = mode == 'no-publisher'
if semantic: mode = 'fixture'
elif test_lock or no_publisher: mode = 'kate'
assert s.parent==pathlib.Path('/tmp') and s.name.startswith('kax-')
assert os.environ["HOME"]==str(s/"home") and os.environ["XDG_RUNTIME_DIR"]==str(s/"r")
assert not os.environ["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/run/user/")
from kwin_mcp import windows
from kwin_mcp.core import AutomationEngine, SessionType
from types import SimpleNamespace
children=[]
def launch(argv, log, env):
    with (s/log).open("wb") as f:
        p=subprocess.Popen(argv,env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
    children.append(p)
    return p
def stop(p):
    if p.poll() is not None: return
    os.killpg(p.pid,signal.SIGTERM)
    try: p.wait(timeout=4)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid,signal.SIGKILL);p.wait(4)
def until(predicate,description,seconds=10):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        r=predicate()
        if r: return r
        time.sleep(.18)
    raise RuntimeError(description)
def broker_exists():
    if k.poll() is not None:
        raise RuntimeError("Private KWin exited: "+(s/"kwin.log").read_text(errors="replace")[-3500:])
    if not bus.name_has_owner('org.kde.KWin'):
        return None
    if not bus.name_has_owner('org.kde.KWin.AxLinkTest'):
        return None
    assert bus.get_name_owner('org.kde.KWin')==bus.get_name_owner('org.kde.KWin.AxLinkTest'), "broker not owned by compositor"
    return True
def lookup(wid):
    try:
        a=windows.run_window_operation('atspi_explicit_target',window_id=wid,env=e,timeout=3)
    except (RuntimeError, ValueError):
        return None
    if a.get("wayland_ax_root") is None:return None
    assert a['window_id']==str(uuid.UUID(wid))
    return {'pid':int(a['pid']),'root':int(a['wayland_ax_root']),'window_id':a['window_id'],'generation':a['wayland_ax_generation']}
def read_root(ref, pending=False):
    if not ref:return None
    p=subprocess.run([sys.executable,str(pathlib.Path(__file__).with_name("reader.py")),str(ref['pid']),str(ref['root'])],
                     env=e,timeout=6,check=True,text=True,capture_output=True)
    r=json.loads(p.stdout)
    if not r:return None
    if lookup(ref['window_id'])!=ref:return None
    return r

def kwin_list(pid=None,title=None):
    r=windows.run_window_operation("list",env=e,timeout=3)
    out=[]
    for row in r["windows"]:
        if title is not None and title.casefold() not in row['caption'].casefold():
            continue
        if pid is not None:
            sx=windows.run_window_operation("atspi_target",window_id=row["window_id"],env=e,timeout=3)
            if sx["pid"]!=pid:continue
        out.append(row)
    return out

def close_exact(id,pid):
    assert isinstance(pid,int) and pid>0
    id=str(uuid.UUID(id))
    prev=windows._script
    def one(*args,**kwargs):
        return prev(*args,**kwargs).splitlines()[0]+f"""
(function(){{
const target=workspace.windowList().find(w=>String(w.internalId).replace(/[{{}}]/g,"").toLowerCase()==="{id}" && Number(w.pid)==={pid});
if(target) target.closeWindow();
callDBus(request.destination,"/org/kwin_mcp/Result","org.kwin_mcp.Result","Complete",
  JSON.stringify({{ok:true,result:{{dispatched:!!target}}}}));
}})();
"""
    windows._script=one
    try:
        with tempfile.TemporaryDirectory(dir=s) as d:
            rv=windows._execute({'op':'list','window_id':'','timeout':3,'script_path':str(pathlib.Path(d)/'close-owned.js')})
        assert rv['ok'] and rv['result']['dispatched']
    finally:windows._script=prev
try:
    kw_env=dict(os.environ)
    kw_env.pop('WAYLAND_DISPLAY',None)
    kw_env.pop('QT_QPA_PLATFORM',None)
    opts=['/usr/bin/kwin_wayland','--virtual','--width','1024','--height','768','--socket',os.environ['WAYLAND_DISPLAY']]
    if not test_lock: opts.append('--no-lockscreen')
    if test_lock: kw_env['DBUS_SYSTEM_BUS_ADDRESS']='unix:path='+str(s/'nonexistent-system-bus')
    k=launch(opts,'kwin.log',kw_env)
    bus=dbus.SessionBus(private=True)
    until(broker_exists,"private KWin broker service missing",14)
    broker=dbus.Interface(bus.get_object('org.kde.KWin.AxLinkTest','/org/kde/KWin/AxLinkTest'),
                           'org.kde.KWin.AxLinkTest')
    print('BROKER: service owned by isolated KWin',flush=True)
    e={**os.environ,'QT_QPA_PLATFORM':'wayland'}
    subprocess.run(['/usr/bin/dbus-update-activation-environment','WAYLAND_DISPLAY','QT_QPA_PLATFORM','XDG_RUNTIME_DIR',
                    'HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME','XDG_STATE_HOME'],env=e,timeout=5,check=True)
    ab=str(dbus.Interface(bus.get_object('org.a11y.Bus','/org/a11y/bus'),'org.a11y.Bus').GetAddress(timeout=5))
    e.update(AT_SPI_BUS_ADDRESS=ab,QT_LINUX_ACCESSIBILITY_ALWAYS_ON='1',QT_QPA_GENERIC_PLUGINS='jamesos-axlink')
    if no_publisher: e.pop('QT_QPA_GENERIC_PLUGINS',None)
    launch(['/usr/libexec/at-spi2-registryd'],'registry.log',e)
    second=None
    if mode=='fixture':
        source=str(b/'build'/'lifecycle_fixture')
        assert pathlib.Path(source).is_file()
        p=launch([source,str(s/'cmd'),'axfixtureA','axfixtureB','axfixtureC'],'fixture.log',e)
        app_label='Identical Qt window'
    elif mode=='kate':
        d=s/'data'/'kate'/'sessions';d.mkdir(mode=0o700,parents=True)
        (d/'QAx.katesession').write_text('[Open MainWindows]\nCount=2\n')
        p=launch(['/usr/bin/kate','--new','--start','QAx'],'kate.log',e)
        app_label='Kate'
    elif mode=='dolphin':
        a=s/'a'/'Twin'; a.mkdir(mode=0o700,parents=True)
        d=s/'b'/'Twin'; d.mkdir(mode=0o700,parents=True)
        (a/'owned-dolphin-alpha.txt').write_text('first owned fixture')
        (d/'owned-dolphin-bravo.txt').write_text('second owned fixture')
        p=launch(['/usr/bin/dolphin','--new-window',str(a)],'dolphin.log',e)
        app_label='Twin'
        until(lambda:kwin_list(p.pid,app_label),'first Dolphin window is missing')
        second=launch(['/usr/bin/dolphin','--new-window',str(d)], 'dolphin-second.log',e)
    elif mode=='quick':
        p=launch(['/usr/bin/systemsettings'],'quick.log',e)
        app_label='System Settings'
    else:raise ValueError(mode)
    owned={p.pid:p}
    if second is not None:owned[second.pid]=second
    def selected_windows():
        return [row for row in kwin_list(title=app_label)
                if windows.run_window_operation('atspi_target',window_id=row['window_id'],env=e,timeout=3)['pid'] in owned]
    if mode=='quick' or no_publisher:
        wanted=1 if mode=='quick' else 2
        rows=until(lambda:(r if len(r:=selected_windows())==wanted else None),'expected test windows missing')
        engine=AutomationEngine()
        engine._session=SimpleNamespace(is_running=True,info=SimpleNamespace(
            session_type=SessionType.VIRTUAL,dbus_address=os.environ["DBUS_SESSION_BUS_ADDRESS"],
            home_dir=None,wayland_socket=os.environ['WAYLAND_DISPLAY']))
        def mcp(wid):
            return json.loads(engine.window_accessibility(wid,max_nodes=18).split("\n",1)[1])
        if no_publisher:
            assert all(lookup(w['window_id']) is None and mcp(w['window_id'])['status']=='unsupported' for w in rows)
            print('NO PUBLISHER PASS: exact mode did not fall back or return either Kate tree without opt-in',flush=True)
        else:
            wid=rows[0]['window_id']
            rt=until(lambda:read_root(lookup(wid)),'Qt Quick app did not expose a matching registered root',15)
            assert mcp(wid)['match']=='compositor_registered_qt_root'
            print('QT QUICK PASS: stock KDE System Settings exact registered top-level',flush=True)
        sys.exit(0)
    def matched_rows():
        rows=selected_windows()
        if len(rows)!=2:return None
        refs=[lookup(w['window_id']) for w in rows]
        if not all(ref and ref['pid'] in owned for ref in refs):return None
        ax=[read_root(ref) for ref in refs]
        if not all(ax):return None
        return list(zip(rows,refs,ax))
    def need_matched():
        if p.poll() is not None:
            raise RuntimeError("Test process exited: "+(s/f'{mode}.log').read_text(errors='replace')[-2000:])
        return matched_rows()
    try: trio=until(need_matched,'two real matching window registrations/root paths were not found',24)
    except RuntimeError:
        print('OWNED DIAGNOSTIC', [(w['caption'],bool(lookup(w['window_id']))) for w in selected_windows()],flush=True);raise
    (a,ar,aa),(d,dr,da)=trio
    engine=AutomationEngine()
    engine._session=SimpleNamespace(is_running=True,info=SimpleNamespace(
        session_type=SessionType.VIRTUAL, dbus_address=os.environ["DBUS_SESSION_BUS_ADDRESS"],
        home_dir=None, wayland_socket=os.environ['WAYLAND_DISPLAY']))
    def exact_mcp(wid):
        return json.loads(engine.window_accessibility(wid,max_depth=3,max_nodes=24).split("\n",1)[1])
    for w in (a,d):
        result=exact_mcp(w['window_id'])
        assert result['status']=='ok' and result['match']=='compositor_registered_qt_root', result
        assert result['window_id']==w['window_id'] and result['labels_included'] is False
    print(f'{mode.upper()} MCP PASS: actual snapshot entry point returned bounded exact trees',flush=True)
    assert a['window_id']!=d['window_id'] and a['caption']==d['caption']
    assert [round(a['geometry'][k]) for k in ('width','height')]==[round(d['geometry'][k]) for k in ('width','height')]
    assert (aa['bus'],aa['path'])!=(da['bus'],da['path'])
    assert not broker.LookupInternal(ar['window_id'],timeout=2)
    assert lookup(str(uuid.uuid4())) is None
    if mode=='fixture':
        assert {aa['token'],da['token']}=={'axfixtureA','axfixtureB'}
    if mode=='dolphin':
        print('DOLPHIN owned markers', [aa['markers'],da['markers']],flush=True)
        # Some Dolphin icon views don't export item labels while unfocused.
        # The independent native close test below is the identity oracle.
    print(f'{mode.upper()} PASS: identical title and size, exact distinct accessible roots, external direct D-Bus caller denied',flush=True)
    print(f'{mode.upper()} CONTEXT: {len({ar["pid"],dr["pid"]})} owning process(es)',flush=True)
    if semantic:
        import contextlib
        import io
        from kwin_mcp.cli import KwinMcpShell

        shell=KwinMcpShell(engine)
        def cli(command, **args):
            buffer=io.StringIO()
            with contextlib.redirect_stdout(buffer):
                shell.onecmd(command+" "+json.dumps(args))
            return json.loads(buffer.getvalue().split("\n",1)[1])
        def find(tree, role):
            if tree["role"] == role: return tree
            return next((x for c in tree["children"] if (x:=find(c,role)) is not None),None)
        def nodes(tree):
            yield tree
            for c in tree["children"]:
                yield from nodes(c)
        def marked_entry(tree):
            entries=[x for x in nodes(tree) if x["role"] in {"text","entry"}]
            eligible=[x for x in entries if "set_text" in x.get("semantic_actions",[])]
            assert len(entries)==2
            assert len(eligible)==1
            assert all(not x.get("action_handle") for x in entries if x is not eligible[0])
            return eligible[0]
        def snap(w):
            return cli("window_accessibility",window_id=w,include_action_handles=True)["tree"]
        def observer():
            old=windows._script
            def js(*args,**kwargs):
                return old(*args,**kwargs).splitlines()[0]+"""
(function () {
const p=workspace.cursorPos;
const w=workspace.activeWindow;
callDBus(request.destination,"/org/kwin_mcp/Result","org.kwin_mcp.Result","Complete",
 JSON.stringify({ok:true,result:{pointer:[p.x,p.y],active:w?String(w.internalId):null}}));
})();
"""
            windows._script=js
            try:
                with tempfile.TemporaryDirectory(dir=s) as scratch:
                    out=windows._execute({"op":"list","window_id":"","timeout":3,
                                           "script_path":str(pathlib.Path(scratch)/"reader.js")})
                    assert out["ok"]
                    return out["result"]
            finally:
                windows._script=old
        state=observer()
        ids=[w["window_id"] for w in (a,d)]
        active=(state["active"] or "").strip("{}").lower()
        target_id=next(x for x in ids if x!=active)
        twin_id=next(x for x in ids if x!=target_id)
        target=snap(target_id)
        button=(find(target,"push button") or find(target,"button"))
        check=find(target,"check box")
        secret=find(target,"password text")
        assert button and check and secret
        assert "press" in button.get("semantic_actions",[]),button
        assert secret.get("redacted") and not secret.get("action_handle")
        assert "private-fixture-value" not in json.dumps(target)
        args=dict(window_id=target_id,action_handle=button["action_handle"],action="press",
                  verify_handle=check["action_handle"],verify_state="checked",verify_present=True)
        if test_lock:
            lock="org.freedesktop.ScreenSaver"
            owner=until(lambda:bus.get_name_owner(lock) if bus.name_has_owner(lock) else None,
                        "private locker missing")
            assert owner==bus.get_name_owner("org.kde.KWin")
            locker=dbus.Interface(bus.get_object(owner,"/ScreenSaver",introspect=False),lock)
            locker.Lock(timeout=2)
            until(lambda:locker.GetActive(timeout=2),"private compositor failed to lock")
            assert cli("window_semantic_action",**args)["dispatched"] is False
            print("SEMANTIC PRIVATE LOCK PASS: semantic action refused before dispatch")
            sys.exit(0)
        wrong=cli("window_semantic_action",**{**args,"window_id":twin_id})
        assert wrong["status"]=="refused" and wrong["dispatched"] is False
        result=cli("window_semantic_action",**args)
        assert result["status"]=="verified",result
        assert cli("window_semantic_action",**args)["status"]=="refused"
        print("SEMANTIC BUTTON PASS: background state changed only in the exact owned window",flush=True)
        target=snap(target_id)
        twin=snap(twin_id)
        assert "checked" in find(target,"check box")["states"]
        assert "checked" not in find(twin,"check box")["states"]
        assert (find(target,"push button") or find(target,"button"))["action_key"]==button["action_key"]
        entry=marked_entry(target)
        typed=dict(window_id=target_id,action_handle=entry["action_handle"],
                   action="set_text",text="Owned fixture: Tea ☕")
        result=cli("window_semantic_action",**typed)
        assert result["status"]=="verified" and typed["text"] not in json.dumps(result),result
        target=snap(target_id)
        entry=marked_entry(target)
        assert cli("window_semantic_action",**{**typed,"action_handle":entry["action_handle"]})["status"]=="refused"
        twin_entry=marked_entry(twin)
        assert twin_entry["action_key"]!=entry["action_key"]
        sibling=cli("window_semantic_action",window_id=twin_id,
                    action_handle=twin_entry["action_handle"], action="set_text",
                    text="Separate owned twin")
        assert sibling["status"]=="verified",sibling
        assert observer()==state,"Focus/cursor changed during semantic actions"
        print("SEMANTIC CLI PASS: exact background button effect and write to empty entry, "
              "wrong window/replay/nonempty/secret guards, active UUID and pointer unchanged")
        sys.exit(0)

    if test_lock:
        lock_name='org.freedesktop.ScreenSaver'
        owner=until(lambda:bus.get_name_owner(lock_name) if bus.name_has_owner(lock_name) else None,
                    'private compositor did not register a lock service')
        assert owner==bus.get_name_owner('org.kde.KWin')
        locker=dbus.Interface(bus.get_object(owner,'/ScreenSaver',introspect=False),lock_name)
        assert not locker.GetActive(timeout=2)
        # The compositor cannot reach the hardware's system bus in this case.
        locker.Lock(timeout=2)
        until(lambda:locker.GetActive(timeout=2),'private virtual compositor did not lock')
        assert lookup(ar['window_id']) is None and lookup(dr['window_id']) is None
        assert exact_mcp(ar['window_id'])['status']=='unsupported'
        print('PRIVATE LOCK PASS: real KWin lock state denied both current roots; no system bus access',flush=True)
        sys.exit(0)

    if mode=='fixture':
        first=next((item for item in trio if item[2]['token']=='axfixtureA'))
        other=next((item for item in trio if item[2]['token']=='axfixtureB'))
        def control(cmd):
            ctrl=s/'cmd.new';ctrl.write_text(cmd);ctrl.rename(s/'cmd')
        first_id=first[0]['window_id']
        control('hide_a')
        until(lambda:lookup(first_id) is None and len(selected_windows())==1,'hidden target still maps')
        assert exact_mcp(first_id)['status']=='unsupported'
        assert lookup(other[0]['window_id'])==other[1]
        control('show_a')
        mapped=until(matched_rows,'remapped fixture did not register again')
        remap=next((item for item in mapped if item[2]['token']=='axfixtureA'))
        assert remap[1]['generation'] != first[1]['generation']
        assert remap[2]['bus']==first[2]['bus'] and remap[2]['path']==first[2]['path']
        assert read_root(first[1]) is None
        assert exact_mcp(remap[0]['window_id'])['status']=='ok'
        print('FIXTURE PASS: unmap revoked the old generation; remap got a fresh registration on the same provider/path',flush=True)
        (a,ar,aa),(d,dr,da)=mapped
    close_exact(a['window_id'],ar['pid'])
    until(lambda:lookup(a['window_id']) is None,'closed UUID still resolves')
    assert exact_mcp(a['window_id'])['status']=='unsupported'
    def only_survivor():
        rows=selected_windows()
        return rows if len(rows)==1 and rows[0]['window_id']==d['window_id'] else None
    until(only_survivor,'expected survivor KWin UUID not found')
    survivor=read_root(dr)
    assert survivor is not None and (survivor['bus'],survivor['path'])==(da['bus'],da['path'])
    def inactive_old_root():
        obs=subprocess.run([sys.executable,str(pathlib.Path(__file__).with_name("reader.py")),str(ar['pid']),str(ar['root'])],
                            env=e,timeout=6,check=True,text=True,capture_output=True)
        info=json.loads(obs.stdout)
        return not info or not info['showing']
    until(inactive_old_root,"closed window's matched AT-SPI root still showing")
    print(f'{mode.upper()} PASS: closing that exact compositor UUID no longer resolves and its tree disappeared or became hidden, survivor unchanged',flush=True)
    spibus=dbus.bus.BusConnection(ab)
    assert spibus.name_has_owner(da['bus'])
    stop(owned[dr['pid']])
    until(lambda:not spibus.name_has_owner(da['bus']),'actual AT-SPI provider owner stayed active')
    until(lambda:lookup(dr['window_id']) is None,'stale mapping after provider disconnect')
    print(f'{mode.upper()} PASS: provider disconnect removed remaining binding',flush=True)
finally:
    for p in reversed(children):stop(p)
