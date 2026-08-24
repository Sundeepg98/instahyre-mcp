"""Profile writes. The first code in this package that changes his account.

The previous build shipped preview-only and said why: the write contract could
not be verified without writing to the live profile that generates his entire
match queue. That was a reasonable place to stop. It is no longer where we
stop, so the caution has to be replaced by something better than nerve.

What replaced it:

**The contract was read, not guessed.** Skills do not ride the profile PATCH at
all. They have their own resource, and the naming actively misleads:
``candidate_skills`` carries the job-search profile, while
``candidate_skill_model`` carries the skills. Both the resource and its method
(``PATCH``, action ``multi_save``) come from the site's own ``$resource``
definition.

**Every write echoes back, verbatim, every row that is meant to survive.** It
was originally unknown whether the server treats ``{objects: [...]}`` as a full
replacement set or as tastypie's ordinary additive ``patch_list``, so the write
was shaped to give the same result under either. It has since been MEASURED, by
adding one canary skill and then sending a payload that omitted it: the row was
removed. **The resource is a full replacement set.** Anything absent from
``objects`` is deleted.

So the echo is not politeness, it is the thing standing between a partial list
and a wiped profile: each surviving skill goes back **exactly as the server
returned it**, in its original order, so the payload's implicit claim -- "these
are all of them" -- is true.

**Removal exists, and it is the same mechanism read the other way.** This module
was add-only for as long as omission was the accident to be prevented. It is no
longer, because the platform caps the list at 20 and this account sits at the
cap, which makes every addition a swap: a skill that is worth adding cannot be
added until one is dropped. ``update_skills`` therefore takes ``remove``, and a
removal is nothing more than a row this method does not copy into ``objects``.
No new verb, no second request -- ``DELETE`` answers 405 on this resource and
could never have been the route.

What removal adds is not machinery but consequence, so the rails are in the
code rather than in the caller's care: exact case-folded name matching (never a
substring), a name that is not on the profile reported rather than silently
doing nothing, a refusal when one request both adds and removes the same skill,
and a hard refusal to empty the list -- on a reverse marketplace a profile with
no skills is not a small profile, it is an unfindable one.

**Nothing is written before a snapshot exists.** :meth:`ProfileWriter.snapshot`
runs first, unconditionally, and writes to disk before the request goes out --
so a restore point survives the process dying mid-write.

**The write verifies itself.** A 200 is not success. Every write reads back and
compares against what was intended, because a silent no-op is the one failure
mode this package exists to refuse.

The site's skills editor actually fires TWO requests: the PATCH above, then a
``PUT candidate_skills/{jsp_id}`` carrying the whole job-search-profile object.
**Only the first is sent here.** The second carries no skills, and on its way
the site's ``saveCareerBreakFields`` NULLs two career-break fields. Skipping it
deviates from browser behaviour in the safe direction -- strictly fewer fields
touched -- and is recorded rather than hidden.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import constants as C
from .cache import Store, default_db_path
from .errors import ApiError, InstahyreError, InvalidFilter
from .http import InstahyreHTTP
from .paths import relativise_prose
from .taxonomy import Taxonomy

log = logging.getLogger("instahyre.profile_write")


class WriteRefused(InstahyreError):
    kind = "write_refused"


class WriteUnverified(InstahyreError):
    """The write went out but the read-back did not confirm it."""

    kind = "write_unverified"


#: Candidate-level scalars, writable by sparse PATCH. This list is short on
#: purpose. Each one is a scalar on the candidate resource itself, which is the
#: only shape the sparse PATCH is VERIFIED for -- four independent frontend call
#: sites PATCH exactly one scalar key and read the same key back.
WRITABLE_SCALARS = {
    "current_company": str,
    "current_designation": str,
    "total_experience": int,
}

#: Fields that live on the job-search-profile sub-object, not the candidate.
#: They are still refused BY THIS PATCH, and that has not changed: the sparse
#: PATCH reaches candidate-level scalars and these are not on the candidate.
#: What changed on 2026-08-24 is that the refusal now has a destination. The jsp
#: contract was read out of the site's own $resource factory, so these fields
#: are writable -- through update_job_search_profile, which PUTs the whole
#: object back with every key echoed. Two doors, each honest about which fields
#: are behind it, rather than one door that guesses.
JSP_LEVEL_FIELDS = {
    "notice_period": "notice period",
    "notice_period_days": "notice period",
    "current_salary": "current salary",
    "job_type": "job type",
    "location_preferences": "preferred locations",
    "industry_types": "preferred industries",
    "job_function": "job function",
    "status": "job-search status",
}

#: The subset of JSP_LEVEL_FIELDS that update_job_search_profile can actually
#: write, so the PATCH refusal points at a real destination for those and only
#: those. Derived from the constant rather than restated, because a second
#: hand-maintained list is a second thing to forget.
#:
#: ``notice_period_days`` is here because a caller may well pass that name --
#: this package published it as a READ field until the bundle constant proved it
#: was an index rather than a day count. It is an alias, not a second field.
_JSP_FIELD_ALIASES = {"notice_period_days": "notice_period"}
_JSP_NOW_WRITABLE = frozenset(
    k
    for k in JSP_LEVEL_FIELDS
    if _JSP_FIELD_ALIASES.get(k, k) in C.JSP_WRITABLE_FIELDS
)


#: What :meth:`ProfileWriter.snapshot` names its files: a unix timestamp, a
#: hyphen, and a lowercase label. Anything else is not an id this server wrote,
#: and a restore is far too destructive to run against a file of unknown origin.
_SNAPSHOT_ID_RE = re.compile(r"[0-9]{1,20}-[a-z0-9-]{1,40}")


def snapshots_dir() -> Path:
    path = default_db_path().parent / "profile_snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ProfileWriter:
    """Snapshot, write, verify, restore."""

    def __init__(self, http: InstahyreHTTP, store: Store, inbound: Any) -> None:
        self.http = http
        self.store = store
        self.inbound = inbound
        # Built here rather than injected so that location validation cannot be
        # switched off by constructing the writer without it. A validator that
        # is absent on some paths is worse than none: it certifies nothing while
        # reading as a guarantee. This one shares the caller's store, so the
        # 308-token location list is fetched at most once per TTL.
        self.taxonomy = Taxonomy(http, store)

    # -- reading what is there --------------------------------------------

    def candidate_id(self) -> int:
        return self.inbound.candidate_id()

    def candidate_uri(self) -> str:
        return f"/api/v1/candidate_misc/profile/candidate/{self.candidate_id()}"

    def read_skills(self) -> list[dict]:
        """The skill rows exactly as the server returns them.

        Returned verbatim, not shaped. A write echoes these back unchanged, and
        the only way to be sure of that is to never have reshaped them.
        """
        payload = self.http.get(C.EP_SKILL_MODEL, params={"limit": 200, "offset": 0})
        if not isinstance(payload, dict) or "objects" not in payload:
            raise ApiError(
                "The skills resource answered without an 'objects' key; its contract has "
                "changed and no write should be attempted against it.",
                path=C.EP_SKILL_MODEL,
            )
        return [o for o in payload.get("objects") or [] if isinstance(o, dict)]

    def skill_names(self) -> list[str]:
        return [s.get("name", "") for s in self.read_skills() if s.get("name")]

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, *, label: str = "auto") -> dict:
        """Write a restore point to disk. Always runs before any write.

        Holds the skill rows verbatim plus the writable scalars. It deliberately
        does NOT hold his phone, email or name: a snapshot is a rollback tool,
        and personal data in a file on disk is a liability that buys nothing.
        """
        return self.take_snapshot(label=label)[1]

    def take_snapshot(self, *, label: str = "auto") -> tuple[dict, dict]:
        """The snapshot, and its summary, as a pair.

        A jsp write needs the RECORD, not the summary: the object it is about to
        replace is the object that was just captured, and re-reading it would
        open a window between the restore point and the write in which the two
        could disagree. Building the body out of the snapshot closes that window
        by construction -- what is written is exactly what can be restored.
        """
        skills = self.read_skills()
        raw = self.inbound.http.get(
            C.EP_PROFILE.format(candidate_id=self.candidate_id())
        )
        scalars = {k: raw.get(k) for k in WRITABLE_SCALARS if k in raw}
        # Captured from the profile read this method ALREADY makes. A second
        # request would be a second point in time, which is the thing a restore
        # point exists to avoid.
        jsp = raw.get("jsp") if isinstance(raw, dict) else None
        if not isinstance(jsp, dict):
            jsp = None

        record = {
            "snapshot_id": f"{int(time.time())}-{label}",
            "taken_at": time.time(),
            "label": label,
            "candidate_skills": skills,
            "scalars": scalars,
            "skill_names": [s.get("name") for s in skills],
            "job_search_profile": jsp,
        }
        path = snapshots_dir() / f"{record['snapshot_id']}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        log.info(
            "profile snapshot written: %s (%d skills, jsp %s)",
            path.name,
            len(skills),
            "captured" if jsp else "ABSENT",
        )
        summary = {
            "snapshot_id": record["snapshot_id"],
            "path": str(path),
            "skills_captured": len(skills),
            "skill_names": record["skill_names"],
            "scalars_captured": sorted(scalars),
            "jsp_captured": bool(jsp),
            "jsp_keys_captured": len(jsp) if jsp else 0,
        }
        return record, summary

    def list_snapshots(self) -> list[dict]:
        out = []
        for path in sorted(snapshots_dir().glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append(
                {
                    "snapshot_id": data.get("snapshot_id", path.stem),
                    "taken_at": data.get("taken_at"),
                    "label": data.get("label"),
                    "skills": data.get("skill_names") or [],
                }
            )
        return out

    def load_snapshot(self, snapshot_id: Optional[str] = None) -> dict:
        """Load a restore point, refusing anything that is not obviously one.

        ``snapshot_id`` arrives raw from an agent-callable tool, so it is
        untrusted input naming a file. Three things are checked, and each of
        them was added because the version without it did real damage in a
        probe: the id has to LOOK like an id, the resolved path has to stay
        INSIDE the snapshots directory, and the record has to actually contain
        skills.

        The third is the one that matters most. ``restore_skills`` deletes every
        row the snapshot does not mention, so a snapshot holding zero skills is
        not a harmless no-op -- it is an instruction to delete all of them. A
        probe with ``snapshot_id="../not-a-snapshot"`` resolved outside this
        directory, read a file that had no skills in it, and deleted all four of
        his. The docstring on ``restore_skills`` claimed that could not happen.
        """
        available = sorted(snapshots_dir().glob("*.json"), reverse=True)
        if not available:
            raise WriteRefused(
                "There is no snapshot to restore from. Snapshots are written "
                "automatically before every write, so an empty set means nothing has "
                "ever been written by this server."
            )
        if snapshot_id is None:
            path = available[0]
        else:
            if not _SNAPSHOT_ID_RE.fullmatch(str(snapshot_id)):
                raise WriteRefused(
                    f"{snapshot_id!r} is not a snapshot id. Ids look like "
                    "'1755780000-pre-skills-write'. Call "
                    "instahyre_list_profile_snapshots to see the real ones.",
                    snapshot_id=str(snapshot_id)[:60],
                )
            directory = snapshots_dir().resolve()
            path = (directory / f"{snapshot_id}.json").resolve()
            # Belt and braces behind the pattern check: even if the pattern is
            # ever loosened, a path that escaped the directory is refused here.
            if path.parent != directory:
                raise WriteRefused(
                    "Refusing to read a snapshot from outside the snapshots directory."
                )
            if not path.exists():
                raise WriteRefused(
                    f"No snapshot {snapshot_id!r}. Call instahyre_list_profile_snapshots."
                )

        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ``{path.name}`` was already safe. ``{exc}`` was not: an OSError
            # here carries the FULL absolute snapshot path, and it carries it
            # in the spelling ``repr()`` produces, because that is how
            # ``OSError.__str__`` renders its filename. Nothing scrubbed this
            # site at all, so unlike the config loader -- which jobcore does
            # scrub, just not in that spelling -- this one leaked the whole
            # layout on every platform. Same renderer as everywhere else, and
            # exact substitution of the two paths this method actually holds;
            # never a hunt for path-shaped text in an error message.
            detail = relativise_prose(str(exc), (path, path.parent))
            raise WriteRefused(
                f"Snapshot {path.name} could not be read as JSON ({detail}). Refusing "
                "to restore from a file this server cannot understand -- a restore "
                "deletes every skill the snapshot does not mention."
            ) from exc

        if not isinstance(record, dict) or not record.get("candidate_skills"):
            raise WriteRefused(
                f"Snapshot {path.name} holds no skills, so restoring from it would "
                "delete every skill on the profile rather than put anything back. "
                "Refusing. If the intention really is to empty the skill list, do it at "
                + C.SITE_BASE
                + "/candidate/profile/ where it is visible.",
                snapshot=path.name,
            )
        return record

    # -- the skills write --------------------------------------------------

    def plan_skills(self, add: list[str], remove: Optional[list[str]] = None) -> dict:
        """Work out exactly what would be sent, and refuse what should not be.

        Runs the full validation path, so a preview that comes back clean means
        the write itself will not fail validation.

        ``remove`` exists because the platform caps the list at 20 and this
        account is AT the cap, which makes every addition a swap. Removal is not
        a new mechanism and no new request shape: the resource is a full
        replacement set (measured), so a row leaves by being omitted from the
        list this method already builds. What removal needs is not machinery but
        RAILS, and they are here rather than in the caller:

        * a name is matched EXACTLY, after case folding -- never as a substring,
          so asking to drop "System Design" cannot take "System Design Patterns"
          with it;
        * a name that is not on the profile is REPORTED, not silently ignored,
          because a typo that quietly removes nothing looks identical to a
          removal that worked;
        * the same name on both lists is refused outright rather than resolved,
          since under a replacement set "remove X and add X" would destroy X's
          row id and re-create it, which is not what either word asked for;
        * the surviving rows keep their original ORDER and their exact bytes,
          and additions land after them, so a diff of the payload against the
          current list shows only the intended departures.
        """
        current = self.read_skills()
        current_names = [s.get("name", "") for s in current if s.get("name")]

        kept, removed_rows, remove_report = self._partition_for_removal(current, remove)
        kept_names = [s.get("name", "") for s in kept if s.get("name")]
        # Additions are measured against what SURVIVES, not against what is
        # there now. That is the whole point of a swap: the slot a removal frees
        # has to be spendable in the same request, or the cap still blocks the
        # addition and the account stays stuck at 20.
        lowered = {n.strip().lower() for n in kept_names}

        requested = [str(s).strip() for s in (add or [])]

        clash = sorted(
            {n.strip().lower() for n in requested if n.strip()}
            & {n.strip().lower() for n in (remove or []) if str(n).strip()}
        )
        if clash:
            raise InvalidFilter(
                "Refusing a request that both adds and removes the same skill: "
                + ", ".join(clash)
                + ". This resource is a full replacement set, so that is not a no-op -- "
                "it would delete the existing row and create a new one with a different "
                "id, losing nothing visible but changing the record for no reason. Ask "
                "for one or the other.",
                field="remove",
            )
        additions: list[str] = []
        already_on_profile: list[str] = []
        repeated_in_request: list[str] = []
        rejected: list[dict] = []
        seen = set(lowered)

        for name in requested:
            if not name:
                rejected.append({"skill": name, "why": "empty"})
                continue
            if len(name) > C.MAX_SKILL_NAME_CHARS:
                rejected.append(
                    {
                        "skill": name[:60],
                        "why": f"longer than the platform's {C.MAX_SKILL_NAME_CHARS}-character limit",
                    }
                )
                continue
            key = name.lower()
            # Two different reasons to skip, reported separately. "Already on
            # the profile" is a no-op; "you listed it twice" is a mistake in the
            # caller's input. Collapsing them into one bucket hides the second.
            if key in lowered:
                already_on_profile.append(name)
                continue
            if key in seen:
                repeated_in_request.append(name)
                continue
            seen.add(key)
            additions.append(name)

        total = len(kept_names) + len(additions)
        over_by = max(0, total - C.MAX_SKILLS)
        dropped_for_cap: list[str] = []
        if over_by:
            dropped_for_cap = additions[len(additions) - over_by :]
            additions = additions[: len(additions) - over_by]

        uri = self.candidate_uri()
        objects = list(kept) + [{"candidate": uri, "name": n} for n in additions]

        plan = {
            "current_skills": current_names,
            "would_add": additions,
            "would_remove": [s.get("name", "") for s in removed_rows],
            "skipped_already_on_profile": already_on_profile,
            "skipped_repeated_in_request": repeated_in_request,
            "rejected": rejected,
            "dropped_over_platform_cap": dropped_for_cap,
            "resulting_skills": kept_names + additions,
            "resulting_skill_count": len(kept_names) + len(additions),
            "platform_cap": C.MAX_SKILLS,
            "would_send": {
                "method": "PATCH",
                "url": C.API_BASE + C.EP_SKILL_MODEL,
                "json_body": {"objects": objects},
                "headers": {
                    "Content-Type": "application/json",
                    C.APPLY_CSRF_HEADER: "<from the csrftoken cookie>",
                },
            },
            "not_sent": (
                "The site also PUTs the whole job-search-profile object afterwards. This "
                "server does not: that request carries no skills, and it NULLs two "
                "career-break fields on the way past."
            ),
        }
        plan.update(remove_report)

        if removed_rows:
            # The claim this key makes has to change when the request removes,
            # because the old text says "nothing is ever removed" and that would
            # now be a lie told by the tool that is doing the removing.
            plan["removal"] = {
                "rows_that_leave": [
                    {"name": s.get("name"), "id": s.get("id")} for s in removed_rows
                ],
                "how": (
                    "By OMISSION. No delete verb is sent -- DELETE answers 405 on this "
                    "resource -- and no extra request is made. The PATCH below carries "
                    "the rows that stay, and the resource is a full replacement set, so "
                    "everything absent from it is dropped by the server."
                ),
                "everything_else_is_echoed_verbatim": (
                    "Every surviving row is byte-identical to what the server returned "
                    "and in its original order. That is what makes this a removal of "
                    "exactly these rows rather than a rewrite of the whole list."
                ),
                "reversible": (
                    "A snapshot is written to disk before the request goes out and "
                    "instahyre_restore_profile puts these rows back. A row that has been "
                    "deleted server-side is restored as a NEW row -- same name, new id -- "
                    "because its old id no longer exists."
                ),
            }
        else:
            plan["add_only"] = (
                "Existing rows are echoed back byte-for-byte as the server returned "
                "them. This matters: the resource is a FULL REPLACEMENT SET -- measured, "
                "by omitting one row and watching it be deleted -- so a payload that "
                "left any current skill out would remove it. Add-only is what makes the "
                "payload's implicit claim ('these are all of them') true."
            )
        return plan

    def _partition_for_removal(
        self, current: list[dict], remove: Optional[list[str]]
    ) -> tuple[list[dict], list[dict], dict]:
        """Split the current rows into the ones that stay and the ones that go.

        Order is preserved on both sides and no row is copied or rebuilt, so the
        kept rows remain the exact objects the server returned. Matching is on
        the folded name and on nothing else: an id is not accepted here, because
        a caller holding a stale id from an earlier read could aim a removal at
        a row that has since become a different skill.
        """
        wanted = [str(n).strip() for n in (remove or [])]
        blank = [n for n in wanted if not n]
        folded: list[str] = []
        repeated: list[str] = []
        seen: set[str] = set()
        for name in wanted:
            if not name:
                continue
            key = name.lower()
            if key in seen:
                repeated.append(name)
                continue
            seen.add(key)
            folded.append(name)

        by_key = {n.lower(): n for n in folded}
        kept: list[dict] = []
        removed: list[dict] = []
        matched: set[str] = set()
        for row in current:
            key = str(row.get("name", "")).strip().lower()
            if key and key in by_key:
                removed.append(row)
                matched.add(key)
            else:
                kept.append(row)

        not_on_profile = [by_key[k] for k in by_key if k not in matched]

        report: dict = {}
        if wanted:
            report["remove_not_on_profile"] = sorted(not_on_profile)
            report["remove_repeated_in_request"] = repeated
            if blank:
                report["remove_blank_entries_ignored"] = len(blank)
        if not_on_profile:
            report["remove_not_on_profile_note"] = (
                "These were asked for and are NOT on the profile, so they remove "
                "nothing. Matching is exact (case-insensitive), never a substring -- "
                "check the spelling against current_skills rather than assuming the "
                "removal happened."
            )
        return kept, removed, report

    def update_skills(
        self,
        add: list[str],
        remove: Optional[list[str]] = None,
        *,
        confirm: bool = False,
    ) -> dict:
        """Add and/or remove skills on the live profile. One PATCH does both.

        A swap is deliberately ONE request rather than a remove call followed by
        an add call. Under a replacement set the two-call version has a window
        between them where the profile is genuinely short a skill, and if the
        second call fails he is left worse off than before with no single thing
        to retry.
        """
        plan = self.plan_skills(add, remove)

        if not confirm:
            plan["executed"] = False
            plan["next_step"] = (
                "Nothing has been sent. Call again with confirm=True to write. A "
                "snapshot is taken automatically first, and instahyre_restore_profile "
                "puts it back."
            )
            if plan["would_remove"]:
                plan["next_step"] += (
                    " Read would_remove first: those rows are DELETED by this write."
                )
            return plan

        if not plan["would_add"] and not plan["would_remove"]:
            raise WriteRefused(
                "Nothing to write: every requested skill is already on the profile, was "
                "rejected, did not fit under the platform's "
                f"{C.MAX_SKILLS}-skill cap, or -- for a removal -- is not on the profile "
                "to begin with. Refusing to send a request that cannot change anything "
                "-- a no-op write is indistinguishable from a broken one."
            )

        # A reverse marketplace finds him BY his skills. An empty list is not a
        # small profile, it is an invisible one, and no legitimate use of this
        # tool ends there -- so it is refused rather than confirm-gated.
        if plan["would_remove"] and not plan["resulting_skills"]:
            raise WriteRefused(
                "Refusing to remove every skill on the profile. Instahyre is a reverse "
                "marketplace: employers search the skill list, so a profile with none is "
                "not a short profile but an unfindable one, and this write would suppress "
                "every future match cycle rather than one application. If that is really "
                "wanted, do it on the website at "
                + C.SITE_BASE
                + "/candidate/profile/ where the consequence is visible."
            )

        if not self.http.cookies.get("csrftoken"):
            raise WriteRefused(
                "Refusing to write without a CSRF token -- Django would reject the "
                "request and the result would be ambiguous. Run instahyre_auth_status."
            )

        snap = self.snapshot(label="pre-skills-write")
        body = plan["would_send"]["json_body"]

        log.warning(
            "writing %d new and removing %d existing skills on the live profile",
            len(plan["would_add"]),
            len(plan["would_remove"]),
        )
        response = self.http.patch(C.EP_SKILL_MODEL, json_body=body)

        # Expect the RESULT, not the sum of what was there and what was asked
        # for. Reading the old list into the expectation would make a removal
        # that silently failed look like a success, since the row it was
        # supposed to drop would still be in `expected` and so never counted as
        # unexpected.
        verification = self._verify_skills(
            expected=set(n.lower() for n in plan["resulting_skills"])
        )
        result = {
            "executed": True,
            "added": plan["would_add"],
            "removed": plan["would_remove"],
            "snapshot_id": snap["snapshot_id"],
            "skills_now": verification["actual"],
            "skill_count_now": len(verification["actual"]),
            "verified": verification["ok"],
            "verified_by": "re-read of GET " + C.EP_SKILL_MODEL + " after the write",
            "response_objects": (
                len(response.get("objects", [])) if isinstance(response, dict) else None
            ),
        }
        if not verification["ok"]:
            result["missing_after_write"] = verification["missing"]
            result["unexpected_after_write"] = verification["unexpected"]
            result["warning"] = (
                "THE WRITE DID NOT VERIFY. The request was accepted but the profile does "
                "not read back as intended. Nothing further has been attempted "
                "automatically -- call instahyre_restore_profile with the snapshot_id "
                "above to put it back, and do not retry the write until the difference "
                "is understood."
            )
            # Named separately because it is the failure a caller is least likely
            # to read out of a generic "unexpected" list, and the most likely to
            # act on wrongly: a removal that did not take means the slot is still
            # occupied and the swap this write existed to perform did not happen.
            survived = sorted(
                n for n in plan["would_remove"]
                if n.lower() in {a.lower() for a in verification["actual"]}
            )
            if survived:
                result["removal_did_not_take"] = survived
            log.error("skills write did not verify: missing=%s", verification["missing"])
        return result

    def _verify_skills(self, *, expected: set[str]) -> dict:
        actual = self.skill_names()
        actual_lower = {n.lower() for n in actual}
        missing = sorted(expected - actual_lower)
        unexpected = sorted(actual_lower - expected)
        return {
            "ok": not missing and not unexpected,
            "actual": actual,
            "missing": missing,
            "unexpected": unexpected,
        }

    # -- restore -----------------------------------------------------------

    def restore_skills(self, snapshot_id: Optional[str] = None, *, confirm: bool = False) -> dict:
        """Put the skill list back to a snapshot. One PATCH does it.

        This used to run a second stage that DELETEd each row the snapshot did
        not contain, because removal was the unverified half of the contract.
        Both halves are now measured, and both measurements killed that stage:

        * ``DELETE`` on a skill row answers **405, Allow: GET,PATCH**. The verb
          is not supported, so the stage could never have worked -- it was a
          fallback that always errored, which is the mirror image of a check
          that can never fail.
        * ``PATCH {objects: [...]}`` **is a full replacement set**, confirmed by
          omitting a row and watching it disappear. So the PATCH alone performs
          the removal, and the second stage had nothing left to do anyway.

        The consequence for callers is the important part: any partial list sent
        to this resource DELETES everything absent from it. That is what makes
        :meth:`update_skills` echo every surviving row back verbatim, and it is
        also the mechanism by which its ``remove`` argument works at all.

        ONE THING A NAIVE RESTORE GETS WRONG, and it is the case that matters
        now that removal exists. A snapshot row for a skill that has since been
        REMOVED still carries the ``id`` and ``resource_uri`` of a row the server
        has deleted. Sending that back asks the server to update something that
        is gone. Whether it tolerates that or rejects the whole payload is not
        known and cannot be learned without a live removal, so this does not bet
        on it: a snapshot row whose id is no longer present is sent in the shape
        Instahyre's own client uses for a NEW skill -- ``{candidate, name}``,
        no id -- which is a request the site demonstrably makes. The skill comes
        back under a new id. That is a real difference from the pre-removal
        state and it is reported as ``recreated`` rather than glossed as a
        perfect rollback.
        """
        snap = self.load_snapshot(snapshot_id)
        target = snap.get("candidate_skills") or []
        target_names = [s.get("name") for s in target if s.get("name")]
        target_ids = {s.get("id") for s in target if s.get("id") is not None}
        current = self.read_skills()
        current_names = [s.get("name") for s in current if s.get("name")]

        # Rows the restore will drop. The PATCH does the dropping, because the
        # payload is a replacement set -- this list exists to SHOW the caller
        # what disappears, not to drive a second request.
        will_drop = [
            s.get("name")
            for s in current
            if s.get("id") is not None and s.get("id") not in target_ids
        ]

        objects, recreated = self._restorable_rows(target, current)

        if not confirm:
            return {
                "executed": False,
                "snapshot_id": snap.get("snapshot_id"),
                "current_skills": current_names,
                "would_restore_to": target_names,
                "would_drop": will_drop,
                "would_recreate": recreated,
                "how": (
                    "One PATCH carrying the snapshot's rows. The resource is a full "
                    "replacement set, so anything absent from that list is removed by "
                    "the same request."
                ),
                "next_step": "Call again with confirm=True to restore.",
            }

        self.snapshot(label="pre-restore")
        self.http.patch(C.EP_SKILL_MODEL, json_body={"objects": objects})

        after = self.skill_names()
        ok = {n.lower() for n in after} == {n.lower() for n in target_names if n}
        result = {
            "executed": True,
            "snapshot_id": snap.get("snapshot_id"),
            "restored_to": target_names,
            "skills_now": after,
            "dropped": will_drop,
            "recreated": recreated,
            "verified": ok,
            "verified_by": "re-read of GET " + C.EP_SKILL_MODEL + " after the restore",
            "note": (
                None
                if ok
                else "The restore did not land exactly. Compare skills_now against "
                "restored_to and finish by hand at "
                + C.SITE_BASE
                + "/candidate/profile/ if needed."
            ),
        }
        if recreated:
            result["recreated_note"] = (
                "These skills were absent from the profile and have been re-created "
                "rather than revived: the snapshot's row ids no longer exist server-side, "
                "so they were sent as new rows and carry NEW ids. The names are back and "
                "that is what employers search on; the ids are not the originals."
            )
        return result

    def _restorable_rows(
        self, target: list[dict], current: list[dict]
    ) -> tuple[list[dict], list[str]]:
        """The payload that puts ``target`` back, and which rows had to be new.

        A snapshot row is echoed VERBATIM when the server still has that id --
        which is the ordinary case, and keeps a restore byte-identical to what
        was captured. A row whose id is gone is rebuilt in the new-skill shape
        instead, because asking the server to update a deleted row is a request
        Instahyre's own client never makes and whose handling is unmeasured.
        """
        live_ids = {row.get("id") for row in current if row.get("id") is not None}
        objects: list[dict] = []
        recreated: list[str] = []
        uri = self.candidate_uri()
        for row in target:
            row_id = row.get("id")
            if row_id is not None and row_id not in live_ids:
                name = row.get("name")
                if not name:
                    continue
                # The candidate is the one this writer derived, never the one
                # copied off a stale snapshot row -- the same rule a brand new
                # skill follows.
                objects.append({"candidate": uri, "name": name})
                recreated.append(name)
            else:
                objects.append(row)
        return objects, recreated

    # -- scalar fields -----------------------------------------------------

    # -- the job-search profile -------------------------------------------
    #
    # This is the block that retires a refusal, so it is worth being precise
    # about what changed. The refusal read: "those need the whole object PUT
    # back, a contract this server has not verified". Both halves were true.
    # The contract is now read out of the site's own $resource factory and its
    # two callers, quoted verbatim in constants.EP_JSP's comment -- so the
    # second half no longer holds, and the first half is the design below.
    #
    # A PUT IS A FULL REPLACEMENT AND AN OMITTED KEY IS A SILENT DELETION.
    # That is not tested here by omitting a key and looking. It is made
    # unreachable: the body is the object the server just returned, with only
    # the named fields replaced, and _guard_no_key_dropped refuses to send
    # anything narrower. The dangerous question is answered by never asking it.

    def read_jsp(self) -> dict:
        """The job-search profile exactly as the server returns it.

        Verbatim and unshaped, for the same reason the skill rows are: this
        object is about to be sent back, and the only way to be certain it goes
        back unchanged is to never have changed it.
        """
        raw = self.inbound.http.get(
            C.EP_PROFILE.format(candidate_id=self.candidate_id())
        )
        jsp = raw.get("jsp") if isinstance(raw, dict) else None
        if not isinstance(jsp, dict) or not jsp:
            raise ApiError(
                "The profile carries no 'jsp' object, so there is nothing to modify "
                "and nothing to echo back. A write built on an absent read would be a "
                "write that invents the object it claims to be updating.",
                path=C.EP_PROFILE,
            )
        if not isinstance(jsp.get("id"), int) or isinstance(jsp.get("id"), bool):
            raise ApiError(
                "The job-search profile has no integer id. The write route addresses "
                "the JSP's own id, not the candidate's, and the two are different "
                "numbers -- so without it there is no safe URL to build.",
                path=C.EP_PROFILE,
            )
        return jsp

    def _validate_jsp_value(self, field: str, value: Any) -> Any:
        """One field, checked against what the platform's own bundle publishes."""
        if field == "notice_period":
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidFilter(
                    "notice_period is an INDEX into the platform's notice-period "
                    "bands, not a number of days. Pass an integer 0-"
                    f"{C.MAX_NOTICE_PERIOD_INDEX}: "
                    + ", ".join(f"{k}={v}" for k, v in C.NOTICE_PERIOD_RANGES.items()),
                    field=field,
                )
            if value not in C.NOTICE_PERIOD_RANGES:
                raise InvalidFilter(
                    f"notice_period {value} is not one of the platform's bands. "
                    "Valid values: "
                    + ", ".join(f"{k}={v}" for k, v in C.NOTICE_PERIOD_RANGES.items())
                    + ". This is an index, not a day count -- 3 means "
                    f"'{C.NOTICE_PERIOD_RANGES[3]}', not three days.",
                    field=field,
                )
            return value

        if field == "status":
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidFilter(
                    "status must be an integer. "
                    + ", ".join(f"{k}={v}" for k, v in C.JOB_SEARCH_STATUS.items()),
                    field=field,
                )
            if value not in C.JOB_SEARCH_STATUS:
                raise InvalidFilter(
                    f"status {value} is not a known job-search status. Valid values: "
                    + ", ".join(f"{k}={v}" for k, v in C.JOB_SEARCH_STATUS.items()),
                    field=field,
                )
            return value

        if field == "job_type":
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidFilter(
                    "job_type must be an integer. "
                    + ", ".join(f"{k}={v}" for k, v in C.JOB_TYPE_NAMES.items()),
                    field=field,
                )
            if value not in C.JOB_TYPE_NAMES:
                raise InvalidFilter(
                    f"job_type {value} is not valid. "
                    + ", ".join(f"{k}={v}" for k, v in C.JOB_TYPE_NAMES.items()),
                    field=field,
                )
            return value

        if field == "current_salary":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidFilter(
                    "current_salary is a number of LAKHS per annum -- the platform "
                    "renders it as the value times 100000. Pass 18 for 18 LPA, not "
                    "1800000.",
                    field=field,
                )
            if not (C.MIN_SALARY_LAKHS <= value <= C.MAX_SALARY_LAKHS):
                raise InvalidFilter(
                    f"current_salary must be between {C.MIN_SALARY_LAKHS} and "
                    f"{C.MAX_SALARY_LAKHS} lakhs per annum, which is the range the "
                    "platform's own form accepts. A figure outside it is almost "
                    "always rupees that were meant to be lakhs.",
                    field=field,
                )
            # The site parseFloats this field on load and sends a number, while
            # the server returns a string. A CHANGED value therefore goes out
            # as a float, matching the browser; an UNTOUCHED one is echoed in
            # whatever type the server gave.
            return float(value)

        if field == "location_preferences":
            if isinstance(value, str) or not isinstance(value, (list, tuple)):
                raise InvalidFilter(
                    "location_preferences is a LIST of location names, even when "
                    "there is one. A bare string would be sent as a list of its "
                    "characters.",
                    field=field,
                )
            if not value:
                # The empty-list refusal, for the same reason skills has one.
                raise WriteRefused(
                    "Refusing to clear every preferred location. Instahyre is a "
                    "reverse marketplace and location is one of the filters employers "
                    "search on, so a profile with no locations is not an open one -- "
                    "it is one that drops out of location-filtered result sets. If "
                    "that is really wanted, do it at "
                    + C.SITE_BASE
                    + "/candidate/profile/ where the consequence is on screen.",
                    fields=[field],
                )
            resolved = []
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise InvalidFilter(
                        "Every preferred location must be a non-empty string.",
                        field=field,
                    )
                token = self.taxonomy.resolve_location(item.strip())
                if token not in resolved:
                    resolved.append(token)
            return resolved

        # Reachable only by calling this method directly. The public path
        # subtracts JSP_SERVER_OWNED_KEYS from the writable set before it gets
        # here, so an unvalidatable field is refused by name upstream. This
        # stays as the backstop for exactly that direct call: a field with no
        # validator must never become a value on the wire.
        raise WriteRefused(
            f"No validator for {field}; refusing to send an unchecked value."
        )

    def _guard_no_key_dropped(self, read: dict, body: dict) -> None:
        """The whole reason this write is safe. Never weaken it.

        A PATCH that forgets a field changes nothing. A PUT that forgets a field
        DELETES it, and on this resource that means silently unsetting part of
        the row employers filter on -- with no error, no warning, and no
        withdraw. The body is built by copying the read, so a missing key can
        only mean the copy is broken; that is a bug worth stopping the write
        for, not a difference worth sending.
        """
        missing = sorted(set(read) - set(body))
        if missing:
            raise WriteRefused(
                "Refusing to send a body that omits "
                + ", ".join(missing)
                + ". This endpoint is a full replacement: a key that is absent from "
                "the payload is DELETED, not left alone. Every key the read returned "
                "must ride the write.",
                fields=missing,
            )
        added = sorted(set(body) - set(read))
        if added:
            raise WriteRefused(
                "Refusing to send "
                + ", ".join(added)
                + ": the read did not return "
                + ("that key" if len(added) == 1 else "those keys")
                + ", so this payload is not the object the server holds. The site "
                "sends the object back exactly as it received it, and a body with an "
                "extra field is a body the platform has never been sent.",
                fields=added,
            )

    def _guard_server_owned(self, read: dict, body: dict) -> None:
        """Server-owned keys ride the write, but may never be changed by it."""
        moved = {
            key: {"read": read.get(key), "body": body.get(key)}
            for key in C.JSP_SERVER_OWNED_KEYS
            if key in read and body.get(key) != read.get(key)
        }
        if moved:
            raise WriteRefused(
                "Refusing to change server-owned "
                + ", ".join(sorted(moved))
                + ". These are derived or assigned by Instahyre. A supplied value "
                "would be either ignored or believed, and nothing visible from here "
                "distinguishes the two.",
                fields=sorted(moved),
            )

    def _build_jsp_body(self, jsp: dict, supplied: dict) -> dict:
        """Copy the read, replace only what was named, then prove that is true."""
        validated = {k: self._validate_jsp_value(k, v) for k, v in supplied.items()}
        body = dict(jsp)
        body.update(validated)
        self._guard_no_key_dropped(jsp, body)
        self._guard_server_owned(jsp, body)
        return body

    def _describe_jsp(self, jsp: dict) -> dict:
        """The four fields a caller actually reasons about, in words."""
        out: dict = {}
        if isinstance(jsp.get("notice_period"), int):
            out["notice_period"] = {
                "index": jsp["notice_period"],
                "means": C.NOTICE_PERIOD_RANGES.get(jsp["notice_period"], "unknown band"),
            }
        if isinstance(jsp.get("status"), int):
            out["job_search_status"] = {
                "code": jsp["status"],
                "means": C.JOB_SEARCH_STATUS.get(jsp["status"], "unknown status"),
            }
        if isinstance(jsp.get("job_type"), int):
            out["job_type"] = {
                "code": jsp["job_type"],
                "means": C.JOB_TYPE_NAMES.get(jsp["job_type"], "unknown"),
            }
        if jsp.get("current_salary") is not None:
            out["current_salary_lakhs"] = jsp["current_salary"]
        if jsp.get("location_preferences") is not None:
            out["preferred_locations"] = list(jsp["location_preferences"])
        return out

    def plan_job_search_profile(self, **changes: Any) -> dict:
        """What a write would send, without sending it. Reads; never writes."""
        supplied = {k: v for k, v in changes.items() if v is not None}
        if not supplied:
            raise InvalidFilter(
                "Nothing to update -- pass at least one of: "
                + ", ".join(C.JSP_WRITABLE_FIELDS),
                field="fields",
            )
        # DERIVED, not read straight off the constant, and a red control is why.
        # A server-owned key added to JSP_WRITABLE_FIELDS -- by an edit, a merge,
        # or a well-meant "this looks useful" -- used to become NAMEABLE: it got
        # past this check and died further in, at _validate_jsp_value's terminal
        # refusal, with an internal-sounding message and a `pragma: no cover`
        # comment that had quietly stopped being true. Subtracting the
        # server-owned set here makes that edit inert instead of merely
        # survivable, and the refusal below still names the field.
        writable = tuple(k for k in C.JSP_WRITABLE_FIELDS if k not in C.JSP_SERVER_OWNED_KEYS)
        unknown = sorted(k for k in supplied if k not in writable)
        if unknown:
            raise WriteRefused(
                "Not writable through this server: "
                + ", ".join(unknown)
                + ". Writable job-search-profile fields are: "
                + ", ".join(writable)
                + ". The rest of the object is echoed back untouched on every write, "
                "and each exclusion has a reason recorded beside "
                "constants.JSP_WRITABLE_FIELDS -- career stage cascades into four "
                "other fields, is_salary_hidden is gated behind a threshold this "
                "account does not meet, is_immediate_joinee has no write site in any "
                "shipped bundle, and the related objects are sent expanded rather "
                "than as ids.",
                fields=unknown,
            )
        jsp = self.read_jsp()
        return self._plan_from(jsp, supplied)

    def _plan_from(self, jsp: dict, supplied: dict) -> dict:
        body = self._build_jsp_body(jsp, supplied)
        changed = {
            key: {"from": jsp.get(key), "to": body[key]}
            for key in supplied
            if body[key] != jsp.get(key)
        }
        unchanged = sorted(k for k in supplied if k not in changed)
        return {
            "would_send": {
                "method": C.JSP_PUT_METHOD,
                "url": C.API_BASE + C.EP_JSP.format(jsp_id=jsp["id"]),
                "json_body": body,
                "headers": {
                    "Content-Type": "application/json",
                    C.APPLY_CSRF_HEADER: "<from the csrftoken cookie>",
                },
            },
            "jsp_id": jsp["id"],
            "would_change": changed,
            "already_at_that_value": unchanged,
            "body_key_count": len(body),
            "read_key_count": len(jsp),
            "reads_as_now": self._describe_jsp(jsp),
            "would_read_as": self._describe_jsp(body),
            "full_replacement": (
                "This endpoint replaces the whole object. Every one of the "
                f"{len(body)} keys the read returned is in the payload, unchanged "
                "except for the fields listed in would_change -- an omitted key here "
                "would be a deletion, so the body is the read with substitutions "
                "rather than a field list."
            ),
        }

    def update_job_search_profile(self, *, confirm: bool = False, **changes: Any) -> dict:
        """Write the job-search profile. One PUT, carrying the whole object.

        On a reverse marketplace these fields are not cosmetic: notice period
        and preferred locations are filters employers search on, so they decide
        whether he appears in a result set at all rather than how he reads once
        he is in one.
        """
        plan = self.plan_job_search_profile(**changes)
        if not confirm:
            plan["executed"] = False
            plan["next_step"] = (
                "Nothing has been sent. Call again with confirm=True to write. A "
                "snapshot is taken automatically first, and "
                "instahyre_restore_profile puts the whole object back."
            )
            return plan

        if not plan["would_change"]:
            raise WriteRefused(
                "Nothing to write: every field supplied already holds that value. "
                "Refusing to send a request that cannot change anything -- a no-op "
                "write is indistinguishable from a broken one, and this one would "
                "still replace the whole object to achieve it."
            )

        if not self.http.cookies.get("csrftoken"):
            raise WriteRefused(
                "Refusing to write without a CSRF token -- Django would reject the "
                "request and the result would be ambiguous. Run instahyre_auth_status."
            )

        # The snapshot's read is the read the body is built from, so the restore
        # point and the payload describe the same instant.
        record, snap = self.take_snapshot(label="pre-jsp-write")
        before = record.get("job_search_profile")
        if not isinstance(before, dict) or not before:
            raise WriteRefused(
                "The snapshot captured no job-search profile, so there would be no "
                "restore point for a write that replaces the whole object. Refusing "
                "to write without one."
            )
        supplied = {k: v for k, v in changes.items() if v is not None}
        final = self._plan_from(before, supplied)
        body = final["would_send"]["json_body"]
        if not final["would_change"]:
            raise WriteRefused(
                "Between the preview and the write the profile already moved to the "
                "requested values. Nothing was sent."
            )

        cid = self.candidate_id()
        log.warning(
            "writing the live job-search profile: %s", sorted(final["would_change"])
        )
        self.http.put(C.EP_JSP.format(jsp_id=before["id"]), json_body=body)

        # The profile is cached for 15 minutes; a stale read would cheerfully
        # "verify" the value we just changed.
        self.store.put("profile", str(cid), None, -1)
        after = self.read_jsp()

        mismatched = {
            key: {"wanted": body[key], "got": after.get(key)}
            for key in final["would_change"]
            if after.get(key) != body[key]
        }
        # The collateral report, and the point of the whole exercise. A full
        # replacement can move fields nobody named -- the server recomputes some
        # of them -- and the only honest way to ship this write is to show what
        # else moved rather than to report the requested fields and stop.
        also_changed = {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted(set(before) | set(after))
            if key not in final["would_change"] and before.get(key) != after.get(key)
        }
        result = {
            "executed": True,
            "updated": sorted(final["would_change"]),
            "changed": final["would_change"],
            "snapshot_id": snap["snapshot_id"],
            "verified": not mismatched,
            "mismatched": mismatched or None,
            "verified_by": "re-read of the profile's jsp object after the write",
            "reads_as_now": self._describe_jsp(after),
            "keys_sent": len(body),
            "also_changed_by_the_server": also_changed or None,
        }
        if also_changed:
            result["collateral_note"] = (
                "These keys were not requested and moved anyway. Some are server-"
                "derived and expected to follow (is_immediate_joinee tracks "
                "notice_period, status_string tracks status). Any OTHER name here is "
                "a finding: it means the full replacement disturbed something, and "
                "the snapshot above is how to put it back."
            )
        if mismatched:
            result["warning"] = (
                "THE WRITE DID NOT VERIFY. The request was accepted but the profile "
                "does not read back as intended. Nothing further has been attempted "
                "-- call instahyre_restore_profile with scope='job_search_profile' "
                "and the snapshot_id above, and do not retry until the difference is "
                "understood."
            )
            log.error("jsp write did not verify: %s", sorted(mismatched))
        return result

    def restore_job_search_profile(
        self, snapshot_id: Optional[str] = None, *, confirm: bool = False
    ) -> dict:
        """Put the whole job-search profile back to a snapshot. One PUT does it.

        Restoring is the same request as writing, which is the one convenience a
        full-replacement resource offers: the snapshot IS a valid body.
        """
        snap = self.load_snapshot(snapshot_id)
        target = snap.get("job_search_profile")
        if not isinstance(target, dict) or not target:
            raise WriteRefused(
                "That snapshot holds no job-search profile. Snapshots taken before "
                "2026-08-24 captured skills and scalars only, so there is nothing to "
                "restore from this one -- and a restore that guessed the missing keys "
                "would delete every key it failed to guess. Use "
                "instahyre_list_profile_snapshots to find one with jsp_captured set."
            )
        if not isinstance(target.get("id"), int) or isinstance(target.get("id"), bool):
            raise WriteRefused(
                "That snapshot's job-search profile carries no integer id, so there "
                "is no URL to restore it to."
            )

        current = self.read_jsp()
        differs = {
            key: {"now": current.get(key), "snapshot": target.get(key)}
            for key in sorted(set(current) | set(target))
            if current.get(key) != target.get(key)
        }
        preview = {
            "snapshot_id": snap.get("snapshot_id"),
            "taken_at": snap.get("taken_at"),
            "would_send": {
                "method": C.JSP_PUT_METHOD,
                "url": C.API_BASE + C.EP_JSP.format(jsp_id=target["id"]),
                "json_body": target,
            },
            "would_revert": differs,
            "reads_as_now": self._describe_jsp(current),
            "would_read_as": self._describe_jsp(target),
        }
        if not confirm:
            preview["executed"] = False
            preview["next_step"] = "Call again with confirm=True to restore."
            return preview
        if not differs:
            raise WriteRefused(
                "The live job-search profile already matches that snapshot. Nothing "
                "to restore, and nothing was sent."
            )
        if not self.http.cookies.get("csrftoken"):
            raise WriteRefused(
                "Refusing to write without a CSRF token. Run instahyre_auth_status."
            )

        self._guard_no_key_dropped(current, target)
        log.warning("restoring the job-search profile from %s", snap.get("snapshot_id"))
        self.http.put(C.EP_JSP.format(jsp_id=target["id"]), json_body=target)
        self.store.put("profile", str(self.candidate_id()), None, -1)
        after = self.read_jsp()
        still_off = {
            key: {"wanted": target.get(key), "got": after.get(key)}
            for key in sorted(set(target) | set(after))
            if after.get(key) != target.get(key)
        }
        return {
            "executed": True,
            "snapshot_id": snap.get("snapshot_id"),
            "reverted": sorted(differs),
            "verified": not still_off,
            "still_differs": still_off or None,
            "verified_by": "re-read of the profile's jsp object after the restore",
            "reads_as_now": self._describe_jsp(after),
        }

    def update_fields(self, *, confirm: bool = False, **fields: Any) -> dict:
        """Sparse PATCH of candidate-level scalars.

        Refuses any field that lives on the job-search-profile sub-object rather
        than guessing: those need the whole object PUT back, which is a wider
        contract than anything verified here.
        """
        body = {k: v for k, v in fields.items() if v is not None}
        if not body:
            raise InvalidFilter(
                "Nothing to update -- pass at least one field.", field="fields"
            )

        refused = {k: JSP_LEVEL_FIELDS[k] for k in body if k in JSP_LEVEL_FIELDS}
        if refused:
            writable_here = sorted(
                {JSP_LEVEL_FIELDS[k] for k in refused if k in _JSP_NOW_WRITABLE}
            )
            message = (
                "Refusing to write "
                + ", ".join(sorted(refused.values()))
                + " through this tool: those live on the job-search-profile "
                "sub-object, not the candidate record, so setting one means PUTting "
                "that whole object back -- a different request to a different "
                "resource. This one is a sparse PATCH and will not pretend otherwise."
            )
            if writable_here:
                message += (
                    " Use instahyre_update_job_search_profile for "
                    + ", ".join(writable_here)
                    + "; it reads the object, replaces only what you name, and echoes "
                    "every other key back so nothing is dropped."
                )
            else:
                message += " Change these at " + C.SITE_BASE + "/candidate/profile/."
            raise WriteRefused(message, fields=sorted(refused))

        unknown = sorted(k for k in body if k not in WRITABLE_SCALARS)
        if unknown:
            raise WriteRefused(
                "Not writable through this server: "
                + ", ".join(unknown)
                + ". Writable fields are: "
                + ", ".join(sorted(WRITABLE_SCALARS))
                + ". Anything absent from that list has an unverified write shape, and "
                "guessing one risks blanking the field."
            )

        for key, value in body.items():
            expected = WRITABLE_SCALARS[key]
            if expected is int and isinstance(value, bool):
                raise InvalidFilter(f"{key} must be a number, not a boolean.", field=key)
            if not isinstance(value, expected):
                raise InvalidFilter(
                    f"{key} must be {expected.__name__}, got {type(value).__name__}.",
                    field=key,
                )

        cid = self.candidate_id()
        path = C.EP_PROFILE_PATCH.format(candidate_id=cid)
        preview = {
            "would_send": {
                "method": "PATCH",
                "url": C.API_BASE + path,
                "json_body": body,
                "headers": {
                    "Content-Type": "application/json",
                    C.APPLY_CSRF_HEADER: "<from the csrftoken cookie>",
                },
            },
            "fields": sorted(body),
        }
        if not confirm:
            preview["executed"] = False
            preview["next_step"] = "Call again with confirm=True to write."
            return preview

        if not self.http.cookies.get("csrftoken"):
            raise WriteRefused(
                "Refusing to write without a CSRF token. Run instahyre_auth_status."
            )

        snap = self.snapshot(label="pre-field-write")
        self.http.patch(path, json_body=body)

        # The profile is cached for 15 minutes; a stale read would cheerfully
        # "verify" the value we just changed. Expire it in the past first.
        self.store.put("profile", str(cid), None, -1)
        raw = self.http.get(C.EP_PROFILE.format(candidate_id=cid))
        mismatched = {
            k: {"wanted": v, "got": raw.get(k)} for k, v in body.items() if raw.get(k) != v
        }
        return {
            "executed": True,
            "updated": sorted(body),
            "snapshot_id": snap["snapshot_id"],
            "verified": not mismatched,
            "mismatched": mismatched or None,
            "verified_by": "re-read of the profile after the write",
        }
