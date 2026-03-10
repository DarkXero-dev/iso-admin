# Maintainer: TechXero <techxero@xerolinux.xyz>

pkgname=xiso-admin
pkgver=1.0.0
pkgrel=1
pkgdesc="XeroLinux ISO Admin Panel — VPS code generator and maintenance control"
arch=('any')
url="https://xerolinux.xyz"
license=('custom:personal')
depends=(
    'python'
    'python-pyqt6'
    'sshpass'
)
optdepends=(
    'python-paramiko: SSH key setup via password (faster alternative to sshpass)'
)
source=(
    "xiso-admin.py"
    "xiso-admin.desktop"
)
sha256sums=(
    'SKIP'
    'SKIP'
)

package() {
    # Main executable — strip .py extension for clean command name
    install -Dm755 "$srcdir/xiso-admin.py" \
        "$pkgdir/usr/bin/xiso-admin"

    # Desktop entry
    install -Dm644 "$srcdir/xiso-admin.desktop" \
        "$pkgdir/usr/share/applications/xiso-admin.desktop"
}
