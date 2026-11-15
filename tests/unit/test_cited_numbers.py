"""The documentation's numbers must match the artefacts they cite.

This is a test rather than a script someone remembers to run because the
failure it guards against is specifically the failure of remembering. A README
in this repo once described a trace it had not been re-read against and
asserted content the committed log did not contain; it was caught by review,
late, and only by chance.

If this fails, either a result file was regenerated and the prose needs
updating, or the prose drifted from the evidence. Both are worth stopping for.
"""

from __future__ import annotations

from scripts.verify_report_numbers import main


def test_every_cited_number_matches_its_artefact(capsys):
    exit_code = main()
    out = capsys.readouterr().out
    assert exit_code == 0, f"documentation disagrees with evals/results/:\n{out}"
    # Guard against the check silently matching nothing, which would pass.
    checked = int(out.split("checked ")[1].split(" ")[0])
    assert checked > 100, f"only {checked} numbers checked; the parser has stopped matching rows"
