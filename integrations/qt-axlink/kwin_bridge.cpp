#include "plugin.h"
#include "window.h"
#include "workspace.h"
#include "wayland/display.h"
#include "wayland/surface.h"
#include "wayland_server.h"
#include "ax-link-server.h"
#include <wayland-server-core.h>
#include <QDBusConnection>
#include <QDBusContext>
#include <QDBusMessage>
#include <QDir>
#include <QFileInfo>
#include <QHash>
#include <QPointer>
#include <QSet>
#include <QUuid>
#include <QVariantMap>

namespace {
constexpr auto objectPath = "/org/kde/KWin/AxLinkTest";
constexpr auto service = "org.kde.KWin.AxLinkTest";
bool privateEnvironment() {
    const QByteArray declared = qgetenv("AXLINK_PRIVATE_RUNTIME");
    return !declared.isEmpty() && declared == qgetenv("XDG_RUNTIME_DIR")
        && declared.startsWith("/tmp/kax-");
}
class AxLinkBroker final : public KWin::Plugin, protected QDBusContext {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.kde.KWin.AxLinkTest")
    struct Binding {
        QPointer<KWin::Window> window;
        QPointer<KWin::SurfaceInterface> surface;
        pid_t pid;
        uint32_t root;
        QUuid generation;
        wl_resource *publisher;
    };
    QHash<KWin::SurfaceInterface *, Binding> m_bindings;
    QSet<wl_resource *> m_resources;
    QSet<KWin::SurfaceInterface *> m_observed;
    wl_global *m_global = nullptr;
public:
    AxLinkBroker() {
        if (!privateEnvironment() || !KWin::waylandServer() || !KWin::waylandServer()->display()) return;
        auto bus = QDBusConnection::sessionBus();
        if (!bus.registerService(service)) return;
        if (!bus.registerObject(objectPath, this, QDBusConnection::ExportAllSlots)) {
            bus.unregisterService(service);
            return;
        }
        m_global = wl_global_create(*KWin::waylandServer()->display(), &jamesos_ax_link_manager_v1_interface, 1, this, bindGlobal);
        if (!m_global) {
            bus.unregisterObject(objectPath);
            bus.unregisterService(service);
        }
    }
    ~AxLinkBroker() override {
        if (!m_global) return;
        auto bus = QDBusConnection::sessionBus();
        bus.unregisterObject(objectPath);
        bus.unregisterService(service);
        wl_global_destroy(m_global);
        while (!m_resources.isEmpty()) wl_resource_destroy(*m_resources.begin());
    }
public slots:
    QVariantMap LookupInternal(const QString &id) const {
        // Only the compositor's pre-existing script context may read the mapping.
        if (!calledFromDBus() || message().service() != QDBusConnection::sessionBus().baseService()) return {};
        if (!m_global || !KWin::waylandServer()
            || KWin::waylandServer()->isScreenLocked() || !KWin::Workspace::self()) return {};
        const QUuid uuid(id);
        if (uuid.isNull()) return {};
        const auto *w = KWin::Workspace::self()->findWindow(uuid);
        if (!w || w->isDeleted() || w->isLockScreen() || !w->surface() || !w->surface()->isMapped()) return {};
        const auto it = m_bindings.constFind(w->surface());
        if (it == m_bindings.cend() || it->window != w || it->surface != w->surface()
            || it->pid != w->pid()) return {};
        for (auto other = m_bindings.cbegin(); other != m_bindings.cend(); ++other)
            if (other != it && other->pid == it->pid && other->root == it->root
                && other->window && other->surface) return {};
        return {{QStringLiteral("pid"), int(it->pid)}, {QStringLiteral("root"), it->root},
                {QStringLiteral("generation"), it->generation.toString(QUuid::WithoutBraces)},
                {QStringLiteral("window_id"), uuid.toString(QUuid::WithoutBraces)}};
    }
private:
    static void bindGlobal(wl_client *c, void *d, uint32_t version, uint32_t id) {
        auto *self = static_cast<AxLinkBroker *>(d);
        if (self->m_resources.size() >= 256) { wl_client_post_no_memory(c); return; }
        auto *r = wl_resource_create(c, &jamesos_ax_link_manager_v1_interface, int(version), id);
        if (!r) { wl_client_post_no_memory(c); return; }
        self->m_resources.insert(r);
        static const struct jamesos_ax_link_manager_v1_interface handlers = {destroy, setRoot, clearRoot};
        wl_resource_set_implementation(r, &handlers, self, [](wl_resource *res) {
            auto *self=static_cast<AxLinkBroker *>(wl_resource_get_user_data(res));
            self->m_resources.remove(res);
            for(auto i=self->m_bindings.begin();i!=self->m_bindings.end();) {
                if(i->publisher==res) i=self->m_bindings.erase(i);
                else ++i;
            }
        });
    }
    static void destroy(wl_client *, wl_resource *r) { wl_resource_destroy(r); }
    static void setRoot(wl_client *c, wl_resource *r, wl_resource *sr, uint32_t oid) {
        if (!sr || oid==0 || wl_resource_get_client(sr)!=c) return;
        auto *self=static_cast<AxLinkBroker *>(wl_resource_get_user_data(r));
        auto *s=KWin::SurfaceInterface::get(sr);
        if (!s || !s->isMapped() || !KWin::Workspace::self()) return;
        auto *w=KWin::Workspace::self()->findWindow([s](const KWin::Window *x) {
            return x && !x->isDeleted() && !x->isLockScreen() && x->surface()==s;
        });
        if (!w) return; // Qt can publish again once the surface has mapped.
        pid_t pid=0; wl_client_get_credentials(c, &pid, nullptr, nullptr);
        if (pid<=0 || pid!=w->pid()) return;
        if (self->m_bindings.size()>=256 && !self->m_bindings.contains(s)) return;
        const auto old=self->m_bindings.constFind(s);
        if(old!=self->m_bindings.cend() && old->window==w && old->pid==pid && old->root==oid && old->publisher==r) {
            jamesos_ax_link_manager_v1_send_root_set(r,sr,oid);
            return;
        }
        if (!self->m_observed.contains(s)) {
            self->m_observed.insert(s);
            QObject::connect(s,&QObject::destroyed,self,[self,s](){
                self->m_bindings.remove(s); self->m_observed.remove(s);
            });
            QObject::connect(s,&KWin::SurfaceInterface::unmapped,self,[self,s](){self->m_bindings.remove(s);});
        }
        if (old==self->m_bindings.cend() || old->window!=w) {
            QObject::connect(w,&KWin::Window::closed,self,[self,s,w](){
                const auto it=self->m_bindings.constFind(s);
                if(it!=self->m_bindings.cend() && it->window==w) self->m_bindings.remove(s);
            });
        }
        self->m_bindings.insert(s,{w,s,pid,oid,QUuid::createUuid(),r});
        jamesos_ax_link_manager_v1_send_root_set(r,sr,oid);
    }
    static void clearRoot(wl_client *c, wl_resource *r, wl_resource *sr) {
        if (!sr || wl_resource_get_client(sr)!=c) return;
        auto *self=static_cast<AxLinkBroker *>(wl_resource_get_user_data(r));
        if (auto *s=KWin::SurfaceInterface::get(sr)) {
            const auto it=self->m_bindings.constFind(s);
            if(it!=self->m_bindings.cend() && it->publisher==r) self->m_bindings.remove(s);
        }
    }
};
class Factory final : public KWin::PluginFactory {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID PluginFactory_iid FILE "kwin-axlink.json")
    Q_INTERFACES(KWin::PluginFactory)
public:
    std::unique_ptr<KWin::Plugin> create() const override {
        if (!privateEnvironment()) return nullptr;
        return std::make_unique<AxLinkBroker>();
    }
};
}
#include "kwin_bridge.moc"
