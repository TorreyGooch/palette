"""Flagging transcriptions that a real recording could not have produced.

The case that started this, from X50zezLFWWI: whisper looped over a speaker's
thesis, writing one sentence three extra times at zero duration and then a
single "word" 19.56 s long, and the span-averaged alignment still passed at
0.8169. These tests rebuild that shape, and pin that ordinary speech -
including a speaker genuinely repeating themselves - is not flagged.
"""
import pytest

from quotesource import timing


def spoken(text, start=0.0, each=0.3, gap=0.05):
    """Words laid out the way a real recording times them."""
    out, t = [], start
    for word in text.split():
        out.append({"word": word, "start": round(t, 3), "end": round(t + each, 3)})
        t += each + gap
    return out


def looped(text, at, times):
    """The same words repeated at one instant, taking no time at all."""
    return [{"word": w, "start": at, "end": at}
            for _ in range(times) for w in text.split()]


PHRASE = ("they're not the same type of biological organism that humans and "
          "animals are but the intentional stance is that")


# -- what it flags ------------------------------------------------------------

def test_the_real_loop_shape_is_flagged():
    words = (spoken("I just think the agents are another such system", 0.0)
             + spoken(PHRASE, 4.0)
             + looped(PHRASE, 12.0, 3)
             + [{"word": "that��be", "start": 12.0, "end": 31.56}])

    flags = timing.timing_flags(words)

    assert flags["suspect"] is True
    assert flags["zero_duration_words"] == 3 * len(PHRASE.split())
    assert flags["longest_word"]["seconds"] == pytest.approx(19.56)
    assert flags["repeated_runs"], "the loop is a repeated run"
    first_zero = len(spoken("I just think the agents are another such system")) \
        + len(PHRASE.split())
    assert flags["repeated_runs"][0]["index"] == first_zero
    assert len(flags["reasons"]) == 3


def test_a_single_zero_duration_duplicate_is_flagged():
    """The smaller one on the same clip: one phrase duplicated once."""
    words = (spoken("and it has helped a lot of people with this", 0.0)
             + looped("helped a lot of people with this", 3.5, 1)
             + spoken("so we kept going", 3.6))

    flags = timing.timing_flags(words)

    assert flags["suspect"] is True
    assert flags["repeated_runs"][0]["text"] == "helped a lot of people with this"


def test_one_absurdly_long_word_is_flagged_on_its_own():
    words = spoken("a perfectly ordinary sentence") + [
        {"word": "hm", "start": 2.0, "end": 9.5}]

    flags = timing.timing_flags(words)

    assert flags["suspect"] is True
    assert flags["reasons"] == ["one word lasts 7.5s ('hm' at word 4)"]


@pytest.mark.parametrize("seconds,flagged", [(3.9, False), (4.1, True)])
def test_the_long_word_threshold(seconds, flagged):
    words = [{"word": "sooo", "start": 0.0, "end": seconds}]
    assert timing.timing_flags(words)["suspect"] is flagged


# -- what it leaves alone -----------------------------------------------------

def test_ordinary_speech_is_not_flagged():
    flags = timing.timing_flags(spoken(PHRASE + " " + PHRASE.upper()))

    assert flags["suspect"] is False
    assert flags["reasons"] == []


def test_a_speaker_repeating_themselves_is_not_a_loop():
    """Real repetition takes time to say; a loop does not."""
    words = spoken("I think that is true I think that is true I think that is true")

    flags = timing.timing_flags(words)

    assert flags["suspect"] is False
    assert flags["repeated_runs"] == []


def test_a_couple_of_zero_duration_words_are_edge_noise():
    words = spoken("the start of it") + [
        {"word": "uh", "start": 1.4, "end": 1.4},
        {"word": "and", "start": 1.4, "end": 1.4}] + spoken("then more", 1.5)

    assert timing.timing_flags(words)["suspect"] is False


def test_nothing_and_malformed_words_do_not_crash():
    assert timing.timing_flags([])["suspect"] is False
    assert timing.timing_flags([])["longest_word"] is None
    odd = [{"word": "x"}, {"word": "y", "start": "?", "end": 1}, "not a dict",
           {"word": "z", "start": 0.0, "end": 0.2}]
    assert timing.timing_flags(odd)["longest_word"]["word"] == "z"


# -- local agreement ----------------------------------------------------------

def transcript_of(words, extra_before="", extra_after=""):
    """expected_for over a word list, as caption segments would give it."""
    def expected_for(start, end):
        inside = [w["word"] for w in words if w["start"] < end and w["end"] > start]
        return " ".join(filter(None, [extra_before, *inside, extra_after]))
    return expected_for


def test_a_clip_shorter_than_one_window_is_left_to_the_span_score():
    words = spoken("only a handful of words here")
    assert timing.window_agreement(words, transcript_of(words)) is None


def test_a_faithful_clip_agrees_everywhere():
    words = spoken(" ".join(f"word{i}" for i in range(90)))

    result = timing.window_agreement(words, transcript_of(words))

    assert result["min"] == 1.0 and result["flagged"] is False


def test_extra_caption_context_does_not_count_against_a_window():
    """Caption segments overlap a window and carry more text than it holds;
    that is the normal case and must not read as disagreement."""
    words = spoken(" ".join(f"word{i}" for i in range(60)))
    padded = transcript_of(words, extra_before="and before that we said a lot",
                           extra_after="and afterwards even more was said")

    assert timing.window_agreement(words, padded)["min"] == 1.0


def test_a_garbled_stretch_is_found_and_located():
    real = spoken(" ".join(f"real{i}" for i in range(90)))
    garbage = spoken(" ".join(f"junk{i}" for i in range(30)), start=real[40]["start"])
    heard = real[:40] + garbage + real[40:]
    # The transcript knows only the real words, at their real times.
    by_time = transcript_of(real)

    result = timing.window_agreement(heard, by_time)

    assert result["flagged"] is True
    assert result["min"] < timing.WINDOW_FLAG
    lo, hi = result["words"]
    assert lo <= 40 and hi >= 40, "the worst window overlaps the garbage"


def test_the_tail_is_always_examined():
    """A step that skipped the last few words would miss a loop at the end."""
    real = spoken(" ".join(f"real{i}" for i in range(47)))
    tail = spoken(" ".join(f"junk{i}" for i in range(28)), start=real[-1]["end"] + 0.1)
    by_time = transcript_of(real)

    result = timing.window_agreement(real + tail, by_time, window=30, step=10)

    assert result["flagged"] is True
    assert result["words"][1] == len(real + tail) - 1
