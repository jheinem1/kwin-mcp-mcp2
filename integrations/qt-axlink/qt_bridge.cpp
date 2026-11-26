#include "ax-link-client.h"
#include <wayland-client-core.h>
#include <wayland-client-protocol.h>
#include <QAccessible>
#include <QGuiApplication>
#include <QHash>
#include <QPlatformSurfaceEvent>
#include <QPointer>
#include <QTimer>
#include <QWindow>
#include <QtGui/qgenericplugin.h>
#include <qpa/qplatformnativeinterface.h>
#include <cstring>

namespace {
class Publisher final : public QObject, private QAccessible::ActivationObserver {
    Q_OBJECT
    wl_display *m_display = nullptr;
    wl_registry *m_registry = nullptr;
    jamesos_ax_link_manager_v1 *m_manager = nullptr;
    quint32 m_globalId = 0;
    QHash<QWindow *, wl_surface *> m_known;
    QHash<wl_surface *, quint32> m_ack;
    QTimer m_poll;
    int m_remaining=0;
public:
    explicit Publisher(QObject *parent=nullptr):QObject(parent) {
        auto *native=QGuiApplication::platformNativeInterface();
        if (!native || QGuiApplication::platformName()!=QStringLiteral("wayland")) return;
        m_display=static_cast<wl_display *>(native->nativeResourceForIntegration("display"));
        if (!m_display) return;
        m_registry=wl_display_get_registry(m_display);
        if (!m_registry) return;
        static const wl_registry_listener listener{newGlobal,removeGlobal};
        wl_registry_add_listener(m_registry,&listener,this);
        wl_display_flush(m_display);
        qGuiApp->installEventFilter(this);
        QAccessible::installActivationObserver(this);
        m_poll.setInterval(160);
        connect(&m_poll,&QTimer::timeout,this,&Publisher::scan);
    }
    ~Publisher() override {
        if (m_registry) QAccessible::removeActivationObserver(this);
        if (m_manager) jamesos_ax_link_manager_v1_destroy(m_manager);
        if (m_registry) wl_registry_destroy(m_registry);
    }
protected:
    bool eventFilter(QObject *target,QEvent *event) override {
        auto *w=qobject_cast<QWindow *>(target);
        const auto type=event->type();
        if (w && (type==QEvent::Hide || (type==QEvent::PlatformSurface
                && static_cast<QPlatformSurfaceEvent *>(event)->surfaceEventType()==QPlatformSurfaceEvent::SurfaceAboutToBeDestroyed))) {
            clear(w);
        }
        if (type==QEvent::Show || type==QEvent::Hide || type==QEvent::PlatformSurface) schedule();
        return false;
    }
private:
    void accessibilityActiveChanged(bool) override {
        QMetaObject::invokeMethod(this,[this]{schedule();},Qt::QueuedConnection);
    }
    void schedule() {
        if (!m_manager) return;
        m_remaining=12;
        if (!m_poll.isActive()) m_poll.start();
    }
    void clear(QWindow *w) {
        if (auto *s=m_known.take(w); s && m_manager) {
            m_ack.remove(s);
            jamesos_ax_link_manager_v1_clear_root(m_manager,s);
            wl_display_flush(m_display);
        }
    }
    static void ack(void *raw,jamesos_ax_link_manager_v1 *,wl_surface *surface,uint32_t id) {
        auto *p=static_cast<Publisher *>(raw);
        if (surface && p->m_known.values().contains(surface)) p->m_ack.insert(surface,id);
    }
    static void newGlobal(void *raw,wl_registry *registry,uint32_t id,const char *name,uint32_t) {
        auto *p=static_cast<Publisher *>(raw);
        if(p->m_manager || std::strcmp(name,jamesos_ax_link_manager_v1_interface.name)!=0) return;
        p->m_manager=static_cast<jamesos_ax_link_manager_v1 *>(wl_registry_bind(registry,id,&jamesos_ax_link_manager_v1_interface,1));
        p->m_globalId=id;
        static const jamesos_ax_link_manager_v1_listener listener{ack};
        jamesos_ax_link_manager_v1_add_listener(p->m_manager,&listener,p);
        p->schedule();
    }
    static void removeGlobal(void *raw, wl_registry *,uint32_t id) {
        auto *p=static_cast<Publisher *>(raw);
        if(!p->m_manager || p->m_globalId!=id) return;
        jamesos_ax_link_manager_v1_destroy(p->m_manager);
        p->m_manager=nullptr; p->m_poll.stop(); p->m_known.clear(); p->m_ack.clear();
    }
    void scan() {
        if (!m_manager || --m_remaining<0) {m_poll.stop(); return;}
        auto *native=QGuiApplication::platformNativeInterface();
        if(!native) {m_poll.stop(); return;}
        const bool active=QAccessible::isActive();
        bool pending=false;
        for(auto *w: QGuiApplication::topLevelWindows()) {
            if(!w) continue;
            const auto kind=w->type();
            if (kind!=Qt::Window && kind!=Qt::Dialog) continue;
            if(!active || !w->isVisible()) {clear(w); continue;}
            auto *s=static_cast<wl_surface *>(native->nativeResourceForWindow("surface",w));
            if(!s) {pending=true; continue;}
            auto *ax=w->accessibleRoot();
            if(!ax || !ax->isValid()) {pending=true; continue;}
            const auto id=QAccessible::uniqueId(ax);
            if(id==0) {pending=true; continue;}
            if (!m_known.contains(w)) {
                connect(w,&QObject::destroyed,this,[this,w]{
                    if (auto *s=m_known.take(w)) m_ack.remove(s);
                });
            }
            m_known.insert(w,s);
            if(m_ack.value(s)==id) continue;
            jamesos_ax_link_manager_v1_set_root(m_manager,s,id);
            pending=true;
        }
        wl_display_flush(m_display);
        if(!pending) m_poll.stop();
    }
};
class PublisherFactory final: public QGenericPlugin {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QGenericPluginFactoryInterface_iid FILE "qt-axlink.json")
public:
    QObject *create(const QString &key,const QString &) override {
        if (key!=QStringLiteral("jamesos-axlink") || qgetenv("AXLINK_PRIVATE_RUNTIME")!=qgetenv("XDG_RUNTIME_DIR")
            || !qgetenv("XDG_RUNTIME_DIR").startsWith("/tmp/kax-")) return nullptr;
        return new Publisher;
    }
};
}
#include "qt_bridge.moc"
