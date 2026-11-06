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


def test_venv_python_is_still_pakito():
    """Nov 6, a friend's PC: Pakito ran from a .venv, whose python.exe only launches the base install's
    python.exe, and that one holds the mic. Pakito took itself for a call and hung up on every conversation."""
    venv = "c:\\users\\acer\\documents\\github\\pakito-jarvis\\.venv\\"
    base = "c:\\users\\acer\\appdata\\local\\programs\\python\\python313\\"
    real = "#C:#Users#Acer#AppData#Local#Programs#Python#Python313#python.exe"
    assert mic_users([(real, NOW - 5, 0)], NOW, venv) == ["python.exe"]          # the old behaviour
    assert mic_users([(real, NOW - 5, 0), (ZOOM, NOW - 5, 0)], NOW, (venv, base)) == ["Zoom.exe"]


def test_own_prefixes_include_the_base_python():
    import sys
    from assistant.calls import own_prefixes
    p = own_prefixes()
    assert any(sys.base_prefix.lower().rstrip("\\/") in x for x in p)
    assert all(x.endswith("\\") for x in p)
