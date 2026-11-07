Name:           mxremap
Version:        1.0.0
Release:        1%{?dist}
URL:            https://github.com/rb314web/mxremap
Summary:        Button remapping, gestures and settings for Logitech MX mice
License:        MIT
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz
Requires:       python3, python3-evdev, python3-gobject, python3-cairo, gtk4, libadwaita
Requires:       libnotify, wl-clipboard, systemd

%description
Graphical tool (GTK4/libadwaita) that remaps buttons of Logitech MX Master and
MX Anywhere mice, adds thumb-button gestures, macros, battery indicator and
notifications, DPI and SmartShift settings. Works on GNOME and KDE.

%prep
%setup -q

%build

%install
install -Dm0644 mxremap.py %{buildroot}%{_datadir}/mxremap/mxremap.py
install -Dm0644 mxremap_gui.py %{buildroot}%{_datadir}/mxremap/mxremap_gui.py
install -Dm0644 mxremap_tray.py %{buildroot}%{_datadir}/mxremap/mxremap_tray.py
install -Dm0755 packaging/mxremap.sh %{buildroot}%{_bindir}/mxremap
install -Dm0755 packaging/mxremap-engine.sh %{buildroot}%{_bindir}/mxremap-engine
install -Dm0644 packaging/mxremap.desktop %{buildroot}%{_datadir}/applications/mxremap.desktop
install -Dm0644 packaging/70-mxremap.rules %{buildroot}%{_prefix}/lib/udev/rules.d/70-mxremap.rules
install -Dm0644 packaging/mxremap-uinput.conf %{buildroot}%{_prefix}/lib/modules-load.d/mxremap-uinput.conf

%post
# Wezsze reguly udev tylko dla podlaczonej myszy (nadpisuja ogolna regule z /usr/lib o tej samej nazwie).
# Bez wykrytej myszy silnik generuje regule ogolna - wtedy plik /etc jest rownowazny pakietowemu.
/usr/bin/python3 %{_datadir}/mxremap/mxremap.py --udev-rules > /etc/udev/rules.d/70-mxremap.rules.new 2>/dev/null \
  && mv -f /etc/udev/rules.d/70-mxremap.rules.new /etc/udev/rules.d/70-mxremap.rules || rm -f /etc/udev/rules.d/70-mxremap.rules.new
udevadm control --reload >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=input --subsystem-match=hidraw --subsystem-match=misc >/dev/null 2>&1 || :
modprobe uinput >/dev/null 2>&1 || :

%postun
if [ $1 -eq 0 ]; then
    rm -f /etc/udev/rules.d/70-mxremap.rules
    udevadm control --reload >/dev/null 2>&1 || :
fi

%files
%ghost %config(noreplace) /etc/udev/rules.d/70-mxremap.rules
%{_bindir}/mxremap-engine
%{_bindir}/mxremap
%{_datadir}/mxremap/
%{_datadir}/applications/mxremap.desktop
%{_prefix}/lib/udev/rules.d/70-mxremap.rules
%{_prefix}/lib/modules-load.d/mxremap-uinput.conf

%changelog
* Sat Nov 07 2026 MX Remap <mxremap@localhost> - 1.0.0-1
- Pierwsze wydanie pakietu
