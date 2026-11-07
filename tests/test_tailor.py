"""Tailoring without the network: a fake Anthropic client, real pdflatex when it's installed."""
import shutil
import sys
import types

import pytest

from jobscraper import tailor

BASE = r"""\documentclass[letterpaper,11pt]{article}
\usepackage[margin=0.6in]{geometry}
\newcommand{\entry}[2]{\textbf{#1} \hfill #2\\}
\begin{document}
\begin{center}{\Large Test Person}\end{center}
\section*{Experience}
\entry{Example Corp, Software Intern}{2026}
\begin{itemize}\item Built a data pipeline in Python. \item Wrote unit tests.\end{itemize}
\end{document}
"""
GOOD = BASE.replace("Built a data pipeline in Python.", "Built a Python ETL data pipeline.")
BROKEN = BASE.replace(r"\end{itemize}", "")


def reply(tex):
    return (f"<analysis>\n- Python data work\n</analysis>\n<changes>\n- Led with the pipeline\n</changes>\n"
            f"<gaps>\n- Spark: mention coursework\n</gaps>\n<latex>\n{tex}\n</latex>")


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def stream(self, **kw):
        self.calls.append(kw)
        text = self.replies.pop(0)
        msg = types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=text)])
        return types.SimpleNamespace(__enter__=None, __exit__=None, get_final_message=lambda: msg)


class _Ctx:
    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        return self.inner

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake(monkeypatch):
    def install(replies):
        client = FakeClient(replies)
        orig = client.stream
        client.stream = lambda **kw: _Ctx(orig(**kw))
        monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda **kw: client))
        return client
    return install


def test_html_to_text_keeps_bullets_and_drops_nav():
    t = tailor.html_to_text("<p>About</p><ul><li>Python &amp; SQL</li><li></li></ul><script>x()</script>")
    assert t == "About\n\n- Python & SQL"


def test_tailor_returns_sections_and_compiles(fake, tmp_path):
    client = fake([reply(GOOD)])
    out = tailor.tailor({"company": "Acme", "title": "Data Intern"}, "We want Python and Spark.", BASE, "", tmp_path)
    assert out["latex"].strip() == GOOD.strip()
    assert out["analysis"] == ["Python data work"] and out["gaps"] == ["Spark: mention coursework"]
    first = client.calls[0]["messages"][0]["content"]
    assert "cache_control" in first[0] and "Spark" in first[1]["text"]  # resume cached, posting after it
    if shutil.which("pdflatex"):
        assert out["compiled"] and out["pages"] == 1 and (tmp_path / "resume.pdf").exists()


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_compile_error_is_sent_back_for_a_fix(fake, tmp_path):
    client = fake([reply(BROKEN), reply(GOOD)])
    out = tailor.tailor({"company": "Acme", "title": "Intern"}, "desc", BASE, "", tmp_path)
    assert out["compiled"] and out["fix_rounds"] == 1
    fix_msg = client.calls[1]["messages"][-1]["content"]
    assert "doesn't compile" in fix_msg


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_no_fix_rounds_when_the_base_resume_cant_build_here(fake, tmp_path):
    base = BASE.replace(r"\begin{document}", "\\usepackage{not-a-real-package}\n\\begin{document}")
    client = fake([reply(base)])
    out = tailor.tailor({"company": "Acme", "title": "Intern"}, "desc", base, "", tmp_path)
    assert out["base_compiles"] is False and out["fix_rounds"] == 0 and len(client.calls) == 1


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_compile_uses_the_tailored_file_not_the_saved_base(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    saved.mkdir()
    (saved / "resume.tex").write_text(r"\documentclass{article}\usepackage{no-such-package}\begin{document}x\end{document}")
    monkeypatch.setattr(tailor, "RESUME_DIR", saved)
    ok, pages, log, _ = tailor.compile_tex(GOOD, tmp_path / "work")
    assert ok and pages == 1, log
