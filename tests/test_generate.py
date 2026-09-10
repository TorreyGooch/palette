"""Asking ComfyUI for a beat's references.

The workflow graph is deliberately not written in Python. The box this talks
to holds krea2, two sizes of flux-2-klein, z-image, anima, a
reference-to-video model, five LoRAs and eleven VAEs — every combination needs
a different text encoder, VAE and sampler, so a graph in code would be a code
change every time you changed your mind about a model.

So the graph is an API-format export from ComfyUI with `{{PROMPT}}` where the
prompt goes, and this module only substitutes, submits and waits. What it owes
the caller is a refusal that says what to do, because the fix is three clicks
in a UI and unguessable from a stack trace.
"""
import json

import pytest

from palette_app import generate
from palette_app.generate import GenerateError

# A minimal API-format graph: two nodes, a prompt slot and a seed slot.
TEMPLATE = json.dumps({
    "3": {"class_type": "KSampler",
          "inputs": {"seed": "{{SEED}}", "steps": 8}},
    "6": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "{{PROMPT}}"}},
}).replace('"{{SEED}}"', "{{SEED}}")     # unquoted, so it parses as a number


@pytest.fixture
def workflows(tmp_path, monkeypatch):
    directory = tmp_path / "workflows"
    directory.mkdir()
    monkeypatch.setenv("PALETTE_WORKFLOW_DIR", str(directory))
    return directory


def write(directory, name, text=TEMPLATE):
    (directory / f"{name}.json").write_text(text, encoding="utf-8")


# -- finding a template ------------------------------------------------------

def test_no_templates_refuses_with_instructions(workflows):
    """Unguessable from a stack trace, so the message carries the fix."""
    with pytest.raises(GenerateError) as raised:
        generate.load_workflow()

    message = str(raised.value)
    assert "Export (API)" in message
    assert "{{PROMPT}}" in message


def test_a_single_template_needs_no_naming(workflows):
    write(workflows, "reference")
    raw, name = generate.load_workflow()
    assert name == "reference" and "{{PROMPT}}" in raw


def test_several_templates_refuse_to_pick_for_you(workflows):
    """Which model rendered a reference is a decision, not a default."""
    write(workflows, "krea")
    write(workflows, "klein")

    with pytest.raises(GenerateError, match="none was named"):
        generate.load_workflow()


def test_an_unknown_name_lists_what_there_is(workflows):
    write(workflows, "krea")

    with pytest.raises(GenerateError) as raised:
        generate.load_workflow("nope")

    assert "krea" in str(raised.value)


def test_a_template_without_the_placeholder_is_refused(workflows):
    """It would render whatever text is baked into the graph and look fine."""
    write(workflows, "broken", json.dumps({"6": {"inputs": {"text": "a cat"}}}))

    with pytest.raises(GenerateError, match="no .* placeholder"):
        generate.load_workflow("broken")


def test_listing_says_whether_a_batch_will_vary(workflows):
    write(workflows, "varying")
    write(workflows, "fixed", json.dumps({"6": {"inputs": {"text": "{{PROMPT}}"}}}))

    listed = {w["name"]: w for w in generate.list_workflows()}

    assert listed["varying"]["varies_by_seed"] is True
    # Legal, and almost always a mistake: three identical images.
    assert listed["fixed"]["varies_by_seed"] is False


def test_a_missing_directory_is_empty_rather_than_an_error(tmp_path,
                                                            monkeypatch):
    monkeypatch.setenv("PALETTE_WORKFLOW_DIR", str(tmp_path / "nope"))
    assert generate.list_workflows() == []


# -- substitution ------------------------------------------------------------

def test_the_prompt_and_seed_land_in_the_graph():
    graph = generate.build_graph(TEMPLATE, "a defeated lobster", 4242)

    assert graph["6"]["inputs"]["text"] == "a defeated lobster"
    assert graph["3"]["inputs"]["seed"] == 4242


def test_the_seed_is_a_number_not_a_string():
    """Substituting before parsing is what allows this, and ComfyUI would
    refuse a graph whose seed arrived quoted."""
    graph = generate.build_graph(TEMPLATE, "x", 7)
    assert isinstance(graph["3"]["inputs"]["seed"], int)


def test_a_prompt_containing_quotes_does_not_break_the_template():
    """An apostrophe or a quote in an ordinary prompt would otherwise produce
    a template that no longer parses, and the error would blame the graph."""
    prompt = 'a "defeated" lobster, 35mm \\ tank light'

    graph = generate.build_graph(TEMPLATE, prompt, 1)

    assert graph["6"]["inputs"]["text"] == prompt


def test_the_editor_format_is_named_rather_than_just_failing(workflows):
    with pytest.raises(GenerateError, match="Export \\(API\\)"):
        generate.build_graph("{not json {{PROMPT}}", "x", 1)


# -- submitting and collecting ----------------------------------------------

@pytest.fixture
def comfy(monkeypatch):
    """A ComfyUI that accepts graphs and finishes them on the second poll."""
    state = {"submitted": [], "polls": 0, "finish_after": 1, "status": "success",
             "images": [{"filename": "ref_00001_.png", "subfolder": "",
                         "type": "output"}]}

    def post(path, body, timeout=30.0):
        assert path == "/prompt"
        state["submitted"].append(body["prompt"])
        return {"prompt_id": f"p{len(state['submitted'])}"}

    def get(path, timeout=30.0):
        state["polls"] += 1
        prompt_id = path.rsplit("/", 1)[-1]
        if state["polls"] <= state["finish_after"]:
            return {}
        return {prompt_id: {"status": {"status_str": state["status"],
                                       "completed": state["status"] == "success"},
                            "outputs": {"9": {"images": state["images"]}}}}

    monkeypatch.setattr(generate, "_post", post)
    monkeypatch.setattr(generate, "_get", get)
    monkeypatch.setattr(generate.time, "sleep", lambda *_: None)
    return state


def test_a_batch_submits_one_graph_per_image(workflows, comfy):
    write(workflows, "reference")

    result = generate.generate("a lobster", count=3)

    assert len(comfy["submitted"]) == 3
    assert result["count"] == 3
    assert result["workflow"] == "reference"


def test_every_image_in_a_batch_gets_its_own_seed(workflows, comfy):
    """Three identical pictures is not a set of alternatives, and asking for
    three is entirely about having alternatives."""
    write(workflows, "reference")

    result = generate.generate("a lobster", count=4)

    seeds = [g["3"]["inputs"]["seed"] for g in comfy["submitted"]]
    assert len(set(seeds)) == 4
    assert result["seeds"] == seeds


def test_a_named_seed_pins_the_first_and_varies_the_rest(workflows, comfy):
    """So a result can be reproduced exactly without collapsing the batch."""
    write(workflows, "reference")

    result = generate.generate("a lobster", count=3, seed=99)

    assert result["seeds"][0] == 99
    assert len(set(result["seeds"])) == 3


def test_the_seed_is_reported_against_each_image(workflows, comfy):
    write(workflows, "reference")

    result = generate.generate("a lobster", count=2, seed=5)

    assert result["images"][0]["seed"] == 5


def test_a_still_queued_job_is_waited_on_rather_than_read_as_empty(workflows,
                                                                    comfy):
    """"Not done" and "done and produced nothing" look identical otherwise."""
    write(workflows, "reference")
    comfy["finish_after"] = 3

    assert generate.generate("a lobster", count=1)["count"] == 1


def test_previews_are_not_mistaken_for_results(workflows, comfy):
    """ComfyUI emits temp images as a graph runs; they are not the output."""
    write(workflows, "reference")
    comfy["images"] = [
        {"filename": "preview.png", "subfolder": "", "type": "temp"},
        {"filename": "ref_00001_.png", "subfolder": "", "type": "output"}]

    result = generate.generate("a lobster", count=1)

    assert [i["filename"] for i in result["images"]] == ["ref_00001_.png"]


def test_a_failed_graph_says_so_rather_than_hanging(workflows, comfy):
    write(workflows, "reference")
    comfy["status"] = "error"

    with pytest.raises(GenerateError, match="ComfyUI reported an error"):
        generate.generate("a lobster", count=1)


def test_a_timeout_says_how_far_it_got_and_that_the_queue_stands(workflows,
                                                                  comfy):
    """The queue is not cancelled, so saying so stops a pointless resubmit."""
    write(workflows, "reference")
    comfy["finish_after"] = 10 ** 6

    with pytest.raises(GenerateError) as raised:
        generate.generate("a lobster", count=2, timeout_s=0)

    assert "not cancelled" in str(raised.value)


def test_an_empty_prompt_is_refused_before_anything_is_queued(workflows, comfy):
    write(workflows, "reference")

    with pytest.raises(GenerateError, match="prompt is required"):
        generate.generate("   ", count=3)

    assert comfy["submitted"] == []


def test_the_batch_is_capped(workflows, comfy):
    """A typo in a count should not occupy the GPU for an hour."""
    write(workflows, "reference")

    assert generate.generate("a lobster", count=500)["count"] <= 12
