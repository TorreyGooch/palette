"""How many requests this project actually sends, and to whom.

Two findings from auditing every path that touches YouTube.

**One episode was never one request.** `record_request` ran once per episode
while the fetch asked for four languages in both manual and automatic form —
up to eight caption downloads plus a metadata call, all recorded as one. A
budget of 30/hour was therefore permitting a few hundred requests an hour,
which is why a limit arrived at what looked like a comfortably safe rate.
Asking for eight tracks to use one is also what makes the fetch fragile:
`writeautomaticsub` for a language a video does not natively carry asks
YouTube to auto-translate on demand, and a video with no English captions at
all drew eight such requests and produced nothing.

**Two paths were not rationed at all.** `pull` downloads a whole episode —
~50 MB, or ~2.5 GB for video — and spent nothing from the budget and never
looked at the cooldown. `transcribe.download_audio` had no rate limit, no
request spacing, no budget and no cooldown check, and it runs in batches. So
a 429 could stop every ingest for six hours while a whisper backfill carried
on pulling gigabytes from the same host.

What is *not* rationed, deliberately: podcast CDNs. They want you to have the
file, and pacing them at YouTube's rate would slow a backfill for nothing.
"""
import pytest

from quotesource import ingest


# -- one track, chosen, instead of eight requested ---------------------------

def info(manual=(), auto=()):
    return {"subtitles": {lang: [{"ext": "json3"}] for lang in manual},
            "automatic_captions": {lang: [{"ext": "json3"}] for lang in auto}}


def test_a_manual_track_is_preferred_over_an_automatic_one():
    assert ingest.pick_caption_track(
        info(manual=["en"], auto=["en"])) == ("en", False)


def test_plain_en_is_preferred_over_a_regional_variant():
    assert ingest.pick_caption_track(
        info(manual=["en-GB", "en", "en-US"])) == ("en", False)


def test_an_automatic_track_is_used_when_there_is_no_manual_one():
    assert ingest.pick_caption_track(info(auto=["en"])) == ("en", True)


def test_an_unlisted_english_variant_still_beats_asking_for_a_translation():
    """One request for a track that exists, rather than a translate request
    for one that does not."""
    assert ingest.pick_caption_track(
        info(manual=["en-GB-oxendict"])) == ("en-GB-oxendict", False)


def test_a_video_with_no_english_captions_asks_for_nothing():
    """The expensive case, and the one that produced nothing for eight
    requests. `writeautomaticsub` for a language the video does not carry is
    a request for an on-demand auto-translation."""
    assert ingest.pick_caption_track(info(manual=["de"], auto=["fr", "es"])) is None


def test_no_captions_at_all_asks_for_nothing():
    assert ingest.pick_caption_track({}) is None


# -- the ledger counts requests, not episodes --------------------------------

@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTESOURCE_DATA", str(tmp_path))
    monkeypatch.setenv("QS_MAX_PER_HOUR", "30")
    monkeypatch.setenv("QS_MAX_PER_DAY", "200")
    monkeypatch.delenv("QS_IGNORE_COOLDOWN", raising=False)
    return tmp_path


def test_several_requests_can_be_charged_at_once(corpus):
    ingest.record_request("fetch", 3)
    assert ingest.budget_state()["day"] == 3


def test_charging_nothing_is_allowed_and_costs_nothing(corpus):
    """An episode whose fetch made exactly one request charges no remainder."""
    ingest.record_request("fetch", 0)
    ingest.record_request("fetch", -1)
    assert ingest.budget_state()["day"] == 0


def test_an_episode_costs_what_it_actually_sent(corpus, monkeypatch):
    """One slot was taken up front; the rest of the fetch is charged after.

    Before this, an episode that sent nine requests was recorded as one.
    """
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("s", "S", "youtube_channel", "https://x")
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: [{"episode_id": "e1", "url": "u",
                                          "title": "t", "duration": 3600}])
    monkeypatch.setattr(ingest, "_fetch_with_backoff",
                        lambda *a, **k: {"ok": True, "_requests": 3})

    ingest.ingest_source(source, quiet=True)

    # one channel walk + three for the episode
    assert ingest.budget_state()["day"] == 4


def test_a_fetch_that_does_not_report_its_cost_still_counts_one(corpus,
                                                                monkeypatch):
    from quotesource import registry

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    source = registry.add_source("s", "S", "youtube_channel", "https://x")
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: [{"episode_id": "e1", "url": "u",
                                          "title": "t", "duration": 3600}])
    monkeypatch.setattr(ingest, "_fetch_with_backoff", lambda *a, **k: {"ok": 1})

    ingest.ingest_source(source, quiet=True)

    assert ingest.budget_state()["day"] == 2


# -- which hosts are rationed ------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc",
    "https://youtu.be/abc",
    "https://rr3---sn-x.googlevideo.com/videoplayback?id=1",
])
def test_youtube_urls_draw_on_the_budget(url):
    assert ingest.is_youtube(url) is True


@pytest.mark.parametrize("url", [
    "https://traffic.libsyn.com/show/ep123.mp3",
    "https://anchor.fm/s/1/podcast/play/x.mp3",
    "",
])
def test_a_podcast_cdn_does_not(url):
    """It wants you to have the file. Pacing it at YouTube's rate is pointless."""
    assert ingest.is_youtube(url) is False


def test_a_lookalike_host_is_not_youtube():
    """Suffix matching has to be on a dot boundary."""
    assert ingest.is_youtube("https://notyoutube.com/watch?v=a") is False


# -- spacing is for bulk, the standoff is for everyone -----------------------

def test_a_slot_without_spacing_still_counts_the_request(corpus):
    from datetime import datetime, timedelta

    ingest.budget_path().write_text(
        f'["{(datetime.now() - timedelta(seconds=5)).isoformat()}"]',
        encoding="utf-8")
    assert ingest.budget_state()["wait_s"] > 0, "it would otherwise wait"

    ingest.await_slot(quiet=True, spacing=False)

    assert ingest.budget_state()["day"] == 2


def test_a_slot_without_spacing_still_refuses_a_spent_day(corpus):
    """Declining to *wait* is not declining to stop."""
    from datetime import datetime, timedelta

    now = datetime.now()
    ingest.budget_path().write_text(
        "[" + ",".join(f'"{(now - timedelta(minutes=n)).isoformat()}"'
                       for n in range(200)) + "]", encoding="utf-8")

    with pytest.raises(ingest.BudgetExhausted):
        ingest.await_slot(quiet=True, spacing=False)


def test_the_downloading_paths_obey_a_cooldown(corpus, monkeypatch):
    """They did not, which made "we stay stopped" untrue.

    A 429 could stop every ingest for six hours while a whisper backfill went
    on pulling whole episodes from the host that had just said stop.
    """
    from quotesource import transcribe

    ingest.begin_cooldown(RuntimeError("HTTP Error 429"), "levin_yt")
    ep = corpus / "ep"
    ep.mkdir()
    (ep / "metadata.json").write_text(
        '{"episode_id": "e1", "url": "https://youtu.be/e1"}', encoding="utf-8")
    monkeypatch.setattr(transcribe, "load_metadata",
                        lambda *_: {"url": "https://youtu.be/e1"})

    with pytest.raises(ingest.InCooldown):
        transcribe.download_audio(ep, quiet=True)


def test_a_cooldown_does_not_stop_a_podcast_download(corpus, monkeypatch):
    """The standoff is with one host, not with the internet."""
    from quotesource import transcribe

    ingest.begin_cooldown(RuntimeError("HTTP Error 429"), "levin_yt")
    ep = corpus / "ep"
    ep.mkdir()
    monkeypatch.setattr(transcribe, "load_metadata",
                        lambda *_: {"audio_url": "https://cdn.libsyn.com/a.mp3"})
    reached = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            reached.append(urls)

    import sys
    import types
    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    transcribe.download_audio(ep, quiet=True)

    assert reached == [["https://cdn.libsyn.com/a.mp3"]]


# -- an on-demand translation is not a track we can fetch --------------------

def tracks(*langs, translated=()):
    """Caption entries as yt-dlp reports them.

    YouTube's timedtext endpoint takes the target language as `tlang`, so a
    URL carrying it is a request to *generate* rather than to fetch.
    """
    out = {}
    for lang in langs:
        url = (f"https://youtube.com/api/timedtext?lang=hi&tlang={lang}"
               if lang in translated
               else f"https://youtube.com/api/timedtext?lang={lang}")
        out[lang] = [{"ext": "json3", "url": url}]
    return out


def test_an_automatic_track_prefers_the_stored_original():
    """YouTube stores one machine transcript and generates the rest from it.

    A bare "en" among the automatic tracks of a non-English video is a
    translation waiting to be made; "-orig" is the file that already exists.
    """
    info = {"subtitles": {},
            "automatic_captions": tracks("en", "en-orig", translated=("en",))}

    assert ingest.pick_caption_track(info) == ("en-orig", True)


def test_a_video_offering_english_only_as_a_translation_asks_for_nothing():
    """The real case: one video refused every run for a fortnight.

    It lists English, serves none, and answers 429 to the request that would
    generate it. Returning None here is what stops it being retried forever.
    """
    info = {"subtitles": {},
            "automatic_captions": tracks("hi", "en", "fr", translated=("en", "fr"))}

    assert ingest.pick_caption_track(info) is None


def test_a_genuine_automatic_english_track_is_still_used():
    """The guard must not cost us the ordinary case."""
    info = {"subtitles": {}, "automatic_captions": tracks("en", "fr",
                                                          translated=("fr",))}

    assert ingest.pick_caption_track(info) == ("en", True)


def test_a_manual_track_is_never_a_translation_and_keeps_plain_en_first():
    """Uploaded tracks are files, so the ordering that suits them is unchanged."""
    info = {"subtitles": tracks("en", "en-orig"), "automatic_captions": {}}

    assert ingest.pick_caption_track(info) == ("en", False)


def test_a_translated_manual_track_is_still_preferred_over_no_track():
    """`subtitles` are uploaded files; nothing there is generated on demand."""
    info = {"subtitles": tracks("en", translated=("en",)),
            "automatic_captions": {}}

    assert ingest.pick_caption_track(info) == ("en", False)


# -- a caption failure must not discard what phase 1 established -------------

@pytest.fixture
def fake_ytdlp(monkeypatch):
    """A yt-dlp whose second phase can be made to fail."""
    import sys
    import types

    state = {"phase2_raises": None, "info": {}, "calls": 0}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            state["calls"] += 1
            if download:
                if state["phase2_raises"]:
                    raise state["phase2_raises"]
                return state["info"]
            return state["info"]

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return state


def fetch(corpus, fake, quiet=True):
    from quotesource import registry

    source = registry.add_source("s", "S", "youtube_channel", "https://x")
    return ingest._fetch_youtube_episode(
        source, {"episode_id": "e1", "url": "https://youtu.be/e1",
                 "title": "t", "duration": 60}, quiet)


def stored(corpus):
    import json

    path = ingest.episode_dir("s", "e1") / "metadata.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def test_a_refused_caption_fetch_still_records_the_episode(corpus, fake_ytdlp):
    """The bug behind a directory that sat empty for a fortnight.

    Phase 1 already established the title, duration and url. Losing all of it
    because the caption fetch was refused meant every later run met the video
    as though it had never been seen — and met it in the same early position,
    where its refusal ended the run before anything else was fetched.
    """
    fake_ytdlp["info"] = {"title": "GRN Inference", "duration": 3072.0,
                          "subtitles": {},
                          "automatic_captions": tracks("en")}
    fake_ytdlp["phase2_raises"] = RuntimeError("HTTP Error 429")

    with pytest.raises(RuntimeError):
        fetch(corpus, fake_ytdlp)

    meta = stored(corpus)
    assert meta is not None, "the episode must not vanish"
    assert meta["title"] == "GRN Inference" and meta["duration"] == 3072.0
    assert meta["status"] == "captions_pending"


def test_a_video_whose_english_is_only_translated_is_queued_for_whisper(
        corpus, fake_ytdlp):
    """Not `captions_pending`, which would retry it forever.

    Nothing here is fetchable: asking produces a refusal rather than a file.
    That is whisper's job, and the ingest loop skips any episode whose status
    is not `captions_pending`, so it stops meeting this one at all.
    """
    fake_ytdlp["info"] = {
        "title": "GRN Inference", "duration": 3072.0, "subtitles": {},
        "automatic_captions": tracks("hi", "en", translated=("en",))}

    meta = fetch(corpus, fake_ytdlp)

    assert meta["status"] == "needs_transcription"
    assert meta["caption_kind"] == "none"
    assert fake_ytdlp["calls"] == 1, "no caption request was ever sent"


def test_that_episode_is_then_skipped_by_a_later_run(corpus, fake_ytdlp,
                                                      monkeypatch):
    """The end of the story: it no longer costs a request or ends a run."""
    fake_ytdlp["info"] = {
        "title": "GRN Inference", "duration": 3072.0, "subtitles": {},
        "automatic_captions": tracks("hi", "en", translated=("en",))}
    fetch(corpus, fake_ytdlp)

    monkeypatch.setattr(ingest.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ingest, "_enumerate_youtube",
                        lambda *a, **k: [{"episode_id": "e1", "url": "u",
                                          "title": "t", "duration": 3072}])
    from quotesource import registry
    result = ingest.ingest_source(registry.get_source("s"), quiet=True)

    assert result["skipped"] == 1 and result["failed"] == 0
    assert result["stopped"] is None


def test_a_normal_fetch_is_unaffected(corpus, fake_ytdlp):
    """The guard must not cost the ordinary path."""
    fake_ytdlp["info"] = {"title": "Ordinary", "duration": 100.0,
                          "subtitles": tracks("en"), "automatic_captions": {}}

    meta = fetch(corpus, fake_ytdlp)

    assert meta["caption_kind"] == "manual"
    assert meta["_requests"] == 3
