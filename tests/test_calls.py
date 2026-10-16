from assistant.calls import CallWatcher, friendly, mic_users

OWN = "c:\\users\\jdsn2\\miniconda3\\envs\\pakito\\"
NOW = 1_800_000_000.0
PAKITO = "#C:#Users#jdsn2#miniconda3#envs#pakito#python.exe"
ZOOM = "#C:#Users#jdsn2#AppData#Roaming#Zoom#bin#Zoom.exe"
WHATSAPP = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm"


def test_friendly_names():
    assert friendly(ZOOM) == "Zoom.exe"
    assert friendly(WHATSAPP) == "WhatsAppDesktop"
    assert friendly("Microsoft.YourPhone_8wekyb3d8bbwe") == "YourPhone"


def test_mic_users_skips_pakito_finished_stale_and_ignored():
    entries = [
        (PAKITO, NOW - 5, 0),                       # Pakito itself: never a call
        (ZOOM, NOW - 60, 0),                        # in a Zoom call right now
        (WHATSAPP, NOW - 3600, NOW - 3000),         # WhatsApp call that ended
        ("#C:#old#Crashed.exe", NOW - 2 * 86400, 0),  # crashed mid-call two days ago: stale
        ("#C:#Program Files#NVIDIA#Broadcast.exe", NOW - 10, 0),
    ]
    assert mic_users(entries, NOW, OWN, ignore=["broadcast.exe"]) == ["Zoom.exe"]
    assert mic_users(entries[:1], NOW, OWN) == []


def test_watcher_reports_start_and_end_once():
    state = {"entries": []}
    seen = []
    w = CallWatcher(lambda on, apps: seen.append((on, apps)), reader=lambda: state["entries"],
                    clock=lambda: NOW, own_prefix=OWN)
    w.poll()
    assert seen == [] and not w.on_call
    state["entries"] = [(PAKITO, NOW - 1, 0), (WHATSAPP, NOW - 2, 0)]
    w.poll()
    w.poll()
    assert seen == [(True, ["WhatsAppDesktop"])] and w.on_call
    state["entries"] = [(WHATSAPP, NOW - 100, NOW - 1)]
    w.poll()
    assert seen[-1] == (False, []) and len(seen) == 2


def test_watcher_survives_registry_errors():
    def boom():
        raise OSError("nope")
    w = CallWatcher(lambda *a: None, reader=boom, own_prefix=OWN)
    w.poll()
    assert not w.on_call
