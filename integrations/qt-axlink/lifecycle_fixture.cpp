#include <QApplication>
#include <QFile>
#include <QFileInfo>
#include <QLabel>
#include <QTimer>
#include <QVBoxLayout>
#include <QWidget>
#include <QWindow>
#include <memory>
#include <vector>

struct Pair {
    std::unique_ptr<QWidget> surface;
    std::unique_ptr<QWidget> witness;
};
static Pair makePair(const QString &token, const QString &privateLabel) {
    Pair p;
    p.surface = std::make_unique<QWidget>();
    p.surface->setWindowTitle(QStringLiteral("Identical Qt window"));
    p.surface->setAccessibleName(QStringLiteral("Identical Qt window"));
    p.surface->setAccessibleIdentifier(token);
    p.surface->setFixedSize(540, 350);
    auto *layout = new QVBoxLayout(p.surface.get());
    auto *label = new QLabel(QStringLiteral("Identical content"), p.surface.get());
    label->setAccessibleName(privateLabel);
    layout->addWidget(label);
    p.surface->show();
    p.witness = std::make_unique<QWidget>();
    p.witness->setWindowTitle(QStringLiteral("Qt AX fixture witness ") + token);
    p.witness->winId();
    p.witness->windowHandle()->setTransientParent(p.surface->windowHandle());
    p.witness->resize(140, 90);
    p.witness->show();
    return p;
}
int main(int argc, char **argv) {
    QApplication app(argc, argv);
    app.setQuitOnLastWindowClosed(false);
    if (argc != 5) return 2;
    const QString control = QString::fromUtf8(argv[1]);
    const QString tokenA = QString::fromUtf8(argv[2]), tokenB = QString::fromUtf8(argv[3]);
    Pair a = makePair(tokenA, QStringLiteral("Owned AX alpha only"));
    Pair b = makePair(tokenB, QStringLiteral("Owned AX bravo only"));
    std::unique_ptr<QWidget> duplicate;
    QTimer timer;
    QObject::connect(&timer, &QTimer::timeout, [&] {
        QFile f(control);
        if (!f.open(QIODevice::ReadOnly)) return;
        const auto cmd = f.readAll().trimmed();
        f.close();
        f.remove();
        if (cmd == "duplicate") {
            duplicate = std::make_unique<QWidget>();
            duplicate->setWindowTitle(QStringLiteral("Unbound test window"));
            duplicate->setAccessibleIdentifier(tokenA);
            duplicate->resize(260, 180);
            duplicate->show();
        }
        if (cmd == "clear_duplicate") duplicate.reset();
        if (cmd == "hide_a") a.surface->hide();
        if (cmd == "show_a") a.surface->show();
        if (cmd == "close_a") { a.witness.reset(); a.surface.reset(); }
        if (cmd == "reopen_a")
            a = makePair(QString::fromUtf8(argv[4]), QStringLiteral("Owned AX alpha reopened"));
    });
    timer.start(100);
    return app.exec();
}
