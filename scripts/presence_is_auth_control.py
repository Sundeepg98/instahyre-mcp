"""A PRESENCE-IS-AUTH build of this server, for showing the honesty guards fail.

WHY THIS FILE IS IN THE REPO
----------------------------
``tests/test_auth_lifecycle.py`` asserts that ``authenticated`` comes from a
real request and never from a cookie sitting in a jar. That assertion is only
worth anything if it is capable of going red -- and a family of checks in these
repos that had never been shown failing turned out, on inspection, to be unable
to fail at all. A test that has never been shown failing is a claim, not a
measurement. The auth contract
(``mcp-servers/_audit/2026-08-23-auth-contract.md``, section 5) requires exactly
one control per repo, and this is it.

THE BUG IT RE-CREATES
---------------------
The one this server actually shipped. ``login_via_browser`` treated "a
``sessionid`` cookie appeared" as "the operator is signed in". Django hands a
``sessionid`` to ANONYMOUS visitors, so the condition was already true the
instant the login page finished loading: the window closed after one poll, the
tool reported ``authenticated: true``, and an ``instahyre_auth_status`` call one
second later returned false.

Here that is reproduced in one function -- ``check_auth`` replaced by a build
that looks at the cookie jar and never makes a request:

    authenticated = bool(http.cookies.get("sessionid"))

Note what that build CANNOT express. It never returns ``None``, so "the check
could not be completed" collapses into a verdict; and it answers true for a
cookie the server would have rejected, so a failed renew looks like a
successful one and overwrites the working session it was meant to protect.
Those are the two failure modes the honesty tests name.

WHICH BINDINGS ARE PATCHED, AND WHY THOSE
-----------------------------------------
``check_auth`` is defined once in ``instahyre_server.session`` and imported BY
NAME into three modules, so rebinding it in ``session`` would reach nobody --
each importer holds its own reference. The three are patched individually:

* ``lifecycle.check_auth`` -- the verdict inside ``instahyre_session_info``.
* ``auth.check_auth`` -- the completion condition inside ``login_via_browser``,
  which is what ``instahyre_reauth`` drives.
* ``server.check_auth`` -- ``instahyre_auth_status``. Patched for completeness:
  the bug shape is server-wide, and a control that fixed only the new surface
  would be describing a narrower defect than the one that shipped.

HOW TO RUN IT
-------------
    PYTHONPATH=scripts pytest tests/test_auth_lifecycle.py -p presence_is_auth_control

    # PowerShell
    $env:PYTHONPATH="scripts"; venv/Scripts/python -m pytest tests/test_auth_lifecycle.py -p presence_is_auth_control

MEASURED 2026-08-23, first against the commit that introduced the lifecycle
surface (``5 failed, 46 passed``), and RE-MEASURED after the wave lead's three
bounces moved the seam to ``reharvest_from_profile``, made a partial logout
report null, and added the session-lapse keys::

    8 failed, 57 passed

    FAILED TestSessionInfoLive::test_a_401_is_reported_as_a_measured_false
    FAILED TestSessionInfoLive::test_an_undetermined_check_is_null_not_false__HONESTY
    FAILED TestReauth::test_a_harvested_cookie_the_endpoint_rejects_is_NOT_a_renewal__HONESTY
    FAILED TestReauth::test_a_failed_renew_leaves_no_file_where_there_was_none
    FAILED TestReauth::test_the_failure_reason_names_the_fallback_tool
    FAILED TestReauthSaysWhichFailureItWas::test_endpoint_said_no
    FAILED TestReauthSaysWhichFailureItWas::test_endpoint_inconclusive_is_null_not_false__HONESTY
    FAILED TestReauthSaysWhichFailureItWas::test_every_failure_names_the_fallback_and_never_returns_an_empty_reason

Read the list, because WHICH eight is the point:

* the three ``__HONESTY`` tests are the contract's rules 1 and 4, and they are
  the reason this file exists;
* ``test_a_401_is_reported_as_a_measured_false`` catches the same substitution
  from the other side -- the endpoint says no and the cookie says yes;
* two reauth tests are the DAMAGE, not just the misreport: under this build a
  failed renew saves the anonymous cookie over the operator's working session
  (``leaves_no_file`` finds a file) and then reports success instead of naming
  the fallback tool;
* the three ``SaysWhichFailureItWas`` entries are the per-outcome reasons that
  depend on a real verdict. Under this build ``endpoint_said_no`` and
  ``endpoint_inconclusive`` both come back as ``renewed``, so the operator is
  told a stale profile was refreshed.

And WHICH 57 survive is equally the point:

* ``verify_live=False`` stays green throughout -- it makes no check at all, so
  a broken check cannot reach it. That guard is real but it is a different
  guard, with its own fakes that raise;
* every ``expired``-is-null and ``session_lapses``-is-null test stays green,
  because expiry is read from the cookie jar and has nothing to do with the
  auth verdict;
* the whole ``cookie_jar`` and ``logout`` sections stay green for the same
  reason;
* ``playwright_missing``, ``no_profile``, ``browser_failed`` and
  ``no_session_cookie`` stay green because every one of them RETURNS BEFORE
  the endpoint is ever asked. A control that broke those too would be a
  control that breaks everything, which points at nothing.

That asymmetry is the property worth having. If the live check is ever
short-circuited again, the honesty tests go red and the rest stay green,
pointing at the defect instead of at everything.

WHOLE-SUITE MEASUREMENT, same build, same day::

    venv/Scripts/python -m pytest tests -q -p presence_is_auth_control
    24 failed, 798 passed

The extra sixteen are all in ``test_auth.py``: the guards written when this bug
was first fixed on the login paths. They are supposed to fail here -- this is
the build they were written against, and their failing is corroboration that
the patch above really does re-create it.

``test_session.py`` stays entirely green under this control, and that is not an
oversight in either direction: those tests call
``instahyre_server.session.check_auth`` DIRECTLY, which is the original
function and not one of the three rebound importer references. They test the
real check; this control replaces what its callers see.
"""


def pytest_sessionstart(session):
    from instahyre_server import auth, lifecycle, server
    from instahyre_server.session import SESSION_COOKIE

    def presence_is_auth(http):
        """The pre-fix verdict: read the jar, ask nobody."""
        has_cookie = bool(http.cookies.get(SESSION_COOKIE))
        return {
            "authenticated": has_cookie,
            "session_cookie_present": has_cookie,
            "checked_against": "the cookie jar (THE BUG: no request is made)",
            "reason": None if has_cookie else "no sessionid cookie is present",
        }

    lifecycle.check_auth = presence_is_auth
    auth.check_auth = presence_is_auth
    server.check_auth = presence_is_auth
    print(
        "\n[presence_is_auth_control] check_auth now reports the PRESENCE of a "
        "sessionid cookie as the verdict, in lifecycle, auth and server -- the "
        "pre-fix bug shape. It can never return None."
    )
