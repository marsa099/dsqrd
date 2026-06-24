"""Desktop notifications with a clickable default action, over dbus (jeepney).

Unlike `notify-send`, this registers a default action and listens for
ActionInvoked, so clicking a notification (or Super+i → invoke) can route back
to the daemon — used to open the channel that sent the message.
"""
import threading
import time

from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection

try:
    from jeepney.low_level import HeaderFields
    _MEMBER = HeaderFields.member
except Exception:  # pragma: no cover - jeepney layout fallback
    _MEMBER = 3

_NOTIF = DBusAddress(
    "/org/freedesktop/Notifications",
    bus_name="org.freedesktop.Notifications",
    interface="org.freedesktop.Notifications",
)


class Notifier:
    def __init__(self, app_name, on_activate):
        self.app = app_name
        self.on_activate = on_activate     # called with the route of a clicked notification
        self.routes = {}                   # notification id -> route (opaque value)
        self.lock = threading.Lock()
        self._send = open_dbus_connection(bus="SESSION")
        self._recv = open_dbus_connection(bus="SESSION")
        rule = MatchRule(type="signal", interface="org.freedesktop.Notifications")
        self._recv.send_and_get_reply(message_bus.AddMatch(rule))
        threading.Thread(target=self._listen, daemon=True).start()

    def notify(self, summary, body, route):
        try:
            msg = new_method_call(
                _NOTIF, "Notify", "susssasa{sv}i",
                (self.app, 0, "", summary, body, ["default", "Open"], {}, -1),
            )
            reply = self._send.send_and_get_reply(msg)
            nid = reply.body[0]
            with self.lock:
                self.routes[nid] = route
        except Exception as e:
            print(f"dsqrd: notify error {e!r}", flush=True)

    def dismiss_all(self):
        """Close all our outstanding notifications (clears the bar badge)."""
        with self.lock:
            ids = list(self.routes.keys())
            self.routes.clear()
        for nid in ids:
            try:
                msg = new_method_call(_NOTIF, "CloseNotification", "u", (nid,))
                self._send.send_and_get_reply(msg)
            except Exception:
                pass

    def _listen(self):
        while True:
            try:
                msg = self._recv.receive()
            except Exception:
                time.sleep(0.5)
                continue
            member = msg.header.fields.get(_MEMBER)
            if member not in ("ActionInvoked", "NotificationClosed"):
                continue
            nid = msg.body[0]
            with self.lock:
                route = self.routes.pop(nid, None)
            if member == "ActionInvoked" and route is not None and self.on_activate:
                try:
                    self.on_activate(route)
                except Exception as e:
                    print(f"dsqrd: notif activate error {e!r}", flush=True)
