"""A PERMISSIVE build of this server's scorer, for showing the guards can fail.

WHY THIS FILE IS IN THE REPO
----------------------------
``tests/test_scoring_policy.py`` asserts that a weight in ``jobhunt.json`` moves
an Instahyre score. That assertion is only worth anything if it is capable of
going red -- and six defects in this family of repos in one week were checks
that could not fail. A test that has never been shown failing is a claim, not a
measurement.

This pytest plugin re-creates the exact bug those tests exist to catch: the
pre-fix ``instahyre_server/scoring.py`` reached jobcore's flat
``compute_fit_score``, which resolves to ``DEFAULT_ENGINE`` -- a singleton built
at import with default everything -- so a policy could be handed in from
anywhere and change nothing. Here that is reproduced by simply discarding the
``policy`` / ``candidate`` / ``policy_rev`` arguments on the way through.

HOW TO RUN IT
-------------
    PYTHONPATH=scripts pytest tests/test_scoring_policy.py -p permissive_scorer_control

    # PowerShell
    $env:PYTHONPATH="scripts"; venv/Scripts/python -m pytest tests/test_scoring_policy.py -p permissive_scorer_control

MEASURED 2026-08-21, against the commit that introduced the config seam::

    6 failed, 40 passed

    FAILED TestDefaultsAreTodaysLiterals::test_a_non_default_scored_result_says_so
    FAILED TestAConfiguredWeightMovesAnInstahyreScore::test_the_function_moves
    FAILED TestAConfiguredWeightMovesAnInstahyreScore::test_the_whole_tool_moves
    FAILED TestTheFileIsRereadWithoutARestart::test_a_constant_length_edit_is_seen
    FAILED TestTheCandidateBlockReachesTheScore::test_candidate_locations_supply...
    FAILED TestVocabularyAdditionsReachTheTaxonomy::test_an_extra_skill_alias_starts...

The 40 that survive are supposed to survive, and reading the list is the point:

* the 15 golden parity cases stay green, because the permissive build IS
  today's behaviour -- that is exactly what "defaults are unchanged" means;
* the ``__CONTROL`` tests stay green, because they assert the OLD path does
  NOT move;
* the Tier-C refusal tests stay green, because refusal happens in jobcore's
  loader, not in this server's scorer.

That asymmetry is the property worth having. If the injection silently stops
working, the guarded tests go red and these stay green.
"""


def pytest_sessionstart(session):
    from instahyre_server import scoring

    real = scoring.score_job

    def ignoring(**kwargs):
        kwargs.pop("policy", None)
        kwargs.pop("candidate", None)
        kwargs.pop("policy_rev", None)
        return real(**kwargs)

    scoring.score_job = ignoring
    print(
        "\n[permissive_scorer_control] score_job now DISCARDS "
        "policy/candidate/policy_rev -- the pre-fix bug shape"
    )
