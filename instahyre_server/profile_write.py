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
from datetime import datetime, timezone
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

    def take_snapshot(
        self, *, label: str = "auto", education: Optional[list[dict]] = None
    ) -> tuple[dict, dict]:
        """The snapshot, and its summary, as a pair.

        A jsp write needs the RECORD, not the summary: the object it is about to
        replace is the object that was just captured, and re-reading it would
        open a window between the restore point and the write in which the two
        could disagree. Building the body out of the snapshot closes that window
        by construction -- what is written is exactly what can be restored.

        ``education`` is PASSED IN rather than read here, and that is deliberate
        in both directions. It gives an education write the same closed window
        the jsp write has -- the rows captured are the rows the body is built
        from -- and it keeps this method's request count at TWO for every path
        that does not write education. A snapshot that always fetched the
        education collection would add a third request to every skills write and
        every jsp write, for a section neither of them can touch.
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
        # ABSENT rather than null when no rows were handed in, so a restore can
        # tell "this snapshot predates education capture" apart from "this
        # account has no education rows". The two need different answers: the
        # first is refused, the second would be a request to delete his whole
        # education section.
        if education is not None:
            record["education"] = education
        path = snapshots_dir() / f"{record['snapshot_id']}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        log.info(
            "profile snapshot written: %s (%d skills, jsp %s, education %s)",
            path.name,
            len(skills),
            "captured" if jsp else "ABSENT",
            len(education) if education is not None else "ABSENT",
        )
        summary = {
            "snapshot_id": record["snapshot_id"],
            "path": str(path),
            "skills_captured": len(skills),
            "skill_names": record["skill_names"],
            "scalars_captured": sorted(scalars),
            "jsp_captured": bool(jsp),
            "jsp_keys_captured": len(jsp) if jsp else 0,
            "education_captured": education is not None,
            "education_rows_captured": len(education) if education is not None else 0,
        }
        return record, summary

    def list_snapshots(self) -> list[dict]:
        out = []
        for path in sorted(snapshots_dir().glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            education = data.get("education")
            out.append(
                {
                    "snapshot_id": data.get("snapshot_id", path.stem),
                    "taken_at": data.get("taken_at"),
                    "label": data.get("label"),
                    "skills": data.get("skill_names") or [],
                    "jsp_captured": bool(data.get("job_search_profile")),
                    # Reported so a caller can pick a snapshot that can actually
                    # answer the scope they mean to restore, rather than
                    # discovering the gap at the refusal.
                    "education_captured": isinstance(education, list),
                    "education_rows": len(education) if isinstance(education, list) else 0,
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

    # -- education ---------------------------------------------------------
    #
    # THE READ AND THE WRITE ARE DIFFERENT SHAPES HERE, which is the one thing
    # that makes this resource unlike the two above. The GET returns
    # ``university`` as an EXPANDED OBJECT; the wire body carries it as a bare
    # resource URI. That is not drift and not a choice made here -- the site
    # applies exactly one transformation on the way out, in
    # ``constructEducationObj``:
    #
    #     obj.university = (obj.university && obj.university.resource_uri)
    #                        ? obj.university.resource_uri
    #                        : obj.university ? obj.university : null;
    #
    # So a pure verbatim echo would NOT reproduce the measured request. The body
    # is therefore "the read, with the university collapse, with the named
    # fields replaced" -- one transformation, quoted from source, confirmed on
    # the wire, and nothing else. ``_guard_education_untouched`` exists to prove
    # that "nothing else" rather than assert it in a comment.
    #
    # EVERY ROW THE READ RETURNED RIDES THE WRITE, not just the edited one.
    # ``{objects: [...]}`` is a MEASURED full replacement set on this platform's
    # other multi_save resource (candidate_skill_model: a row omitted from the
    # list was deleted), so on that reading an omitted ROW here is a deletion of
    # an education entry. Education's envelope also carries ``deleted_objects``,
    # which skills has no equivalent of, and a resource with an explicit removal
    # channel has less need of removal-by-omission -- so the sibling's
    # measurement does not transfer cleanly and the semantics are genuinely
    # unknown. Sending every row is correct under BOTH readings, which is why it
    # is the rule rather than a preference.

    def read_education(self) -> list[dict]:
        """The education rows exactly as the server returns them.

        Verbatim and unshaped, for the same reason the skill rows are: these
        rows are about to be sent back, and the only way to be certain of what
        goes back is to never have reshaped it.
        """
        payload = self.http.get(C.EP_EDUCATION, params={"limit": 200, "offset": 0})
        if not isinstance(payload, dict) or "objects" not in payload:
            raise ApiError(
                "The education resource answered without an 'objects' key; its "
                "contract has changed and no write should be attempted against it.",
                path=C.EP_EDUCATION,
            )
        rows = [o for o in payload.get("objects") or [] if isinstance(o, dict)]
        if not rows:
            raise ApiError(
                "The education collection came back empty. There is no row to modify "
                "and nothing to echo back, and a write built on an absent read would "
                "invent the rows it claims to be updating. Add an education entry at "
                + C.SITE_BASE
                + "/candidate/profile/ first.",
                path=C.EP_EDUCATION,
            )
        return rows

    @staticmethod
    def _collapse_university(value: Any) -> Any:
        """The site's own university transformation, mirrored branch for branch.

        The middle branch is the one worth not simplifying away: a dict with NO
        ``resource_uri`` is passed through UNCHANGED rather than nulled. That is
        the custom-institute case -- ``updateCustomUniversity`` exists precisely
        because a typed university comes back with a uri it did not have -- and
        collapsing it to null here would silently unset the institute on a row
        this tool was never asked to touch.
        """
        if isinstance(value, dict):
            uri = value.get("resource_uri")
            return uri if uri else value
        return value if value else None

    def _validate_education_value(self, field: str, value: Any) -> Any:
        """One field, checked against what the platform's own bundle publishes."""
        if field == "graduation_year":
            if isinstance(value, bool):
                raise InvalidFilter(
                    "graduation_year must be a year, not a boolean.", field=field
                )
            text = str(value).strip()
            if not text or not text.lstrip("-").isdigit():
                raise InvalidFilter(
                    f"graduation_year must be a four-digit year, got {value!r}. The "
                    "site's own form validates it with parseInt and offers a fixed "
                    "list of years.",
                    field=field,
                )
            year = int(text)
            # datetime rather than the module-level ``time``, which several test
            # modules monkeypatch with a hand-cranked clock that has no
            # gmtime. A validator that raises AttributeError under an unrelated
            # fixture is a validator that gets deleted.
            latest = (
                datetime.now(timezone.utc).year
                + C.EDUCATION_GRADUATION_YEAR_LOOKAHEAD
            )
            if not (C.EDUCATION_MIN_GRADUATION_YEAR <= year <= latest):
                raise InvalidFilter(
                    f"graduation_year {year} is outside the range the platform's own "
                    f"year list offers ({C.EDUCATION_MIN_GRADUATION_YEAR} to {latest}). "
                    "Both bounds are read from the shipped getYears(); the upper one is "
                    "this year plus "
                    f"{C.EDUCATION_GRADUATION_YEAR_LOOKAHEAD}, which is what lets a "
                    "current student record a graduation that has not happened yet.",
                    field=field,
                )
            # A STRING, because that is what the wire carried -- "2021", not
            # 2021, even though the page's own option values are numbers. Same
            # rule as the jsp's decimals: a CHANGED value matches the browser, an
            # UNTOUCHED one is echoed in the server's own type.
            return str(year)

        if field in ("gpa", "grading_scale"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidFilter(
                    f"{field} must be a number, got {type(value).__name__}.",
                    field=field,
                )
            if value <= 0:
                raise InvalidFilter(
                    f"{field} must be greater than zero. Clearing it back to null is "
                    "not expressible through this tool -- None means 'not supplied' "
                    "here, exactly as it does on the job-search-profile write -- so a "
                    "zero would be a value, not an erasure.",
                    field=field,
                )
            if value > C.EDUCATION_GPA_MAX:
                raise InvalidFilter(
                    f"{field} of {value} is above {C.EDUCATION_GPA_MAX}, which covers "
                    "every scale this platform's users plausibly use (4.0, 10.0 and "
                    "percentage). No bundle publishes a limit for this field, so this "
                    "bound is a sanity check chosen HERE and is not the platform's -- "
                    "it exists to catch a decimal in the wrong place, not to describe "
                    "a rule Instahyre enforces.",
                    field=field,
                )
            return value

        # Reachable only by calling this method directly: the public path
        # refuses an unknown field by name first. This stays as the backstop for
        # exactly that direct call -- a field with no validator must never
        # become a value on the wire.
        raise WriteRefused(
            f"No validator for {field}; refusing to send an unchecked value."
        )

    def _guard_education_keys(self, read_row: dict, body_row: dict) -> None:
        """Same law as _guard_no_key_dropped, on a row instead of an object.

        Whether an omitted KEY is a deletion on this resource is not measured
        (unlike the jsp's PUT, where it certainly is), and this guard is what
        makes the question moot rather than answered: the body is built by
        copying the read, so a missing key can only mean the copy is broken.
        A key the read did NOT return is refused for the mirror reason -- a body
        carrying a field the server never sent is a body the platform has never
        been sent.
        """
        missing = sorted(set(read_row) - set(body_row))
        if missing:
            raise WriteRefused(
                "Refusing to send an education row that omits "
                + ", ".join(missing)
                + ". Every key the read returned must ride the write: whether this "
                "resource treats an omitted key as a deletion is NOT measured, and a "
                "body that never omits one does not need the answer.",
                fields=missing,
            )
        added = sorted(set(body_row) - set(read_row))
        if added:
            raise WriteRefused(
                "Refusing to send "
                + ", ".join(added)
                + ": the read did not return "
                + ("that key" if len(added) == 1 else "those keys")
                + " on this education row, so this payload is not the row the server "
                "holds. The site echoes the row back as it received it, and a body "
                "with an extra field is a body the platform has never been sent.",
                fields=added,
            )

    def _guard_education_untouched(
        self, read_row: dict, body_row: dict, named: set
    ) -> None:
        """Exactly one transformation is permitted, and it is checked by name.

        This is the guard that makes the university collapse safe to perform at
        all. Without it, "the body is the read plus one measured transformation"
        is a claim in a docstring; with it, any SECOND transformation -- a
        normalised current_degree, a re-derived candidate uri, a stringified id
        -- stops the write instead of reaching his profile.
        """
        expected_university = self._collapse_university(read_row.get("university"))
        if "university" in read_row and body_row.get("university") != expected_university:
            raise WriteRefused(
                "The university field was transformed in a way this server does not "
                "perform. Exactly one transformation is allowed on an education row: "
                "the expanded object the GET returns is collapsed to its resource_uri, "
                "which is what constructEducationObj does and what the captured request "
                "carried. Anything else is unmeasured.",
                fields=["university"],
            )
        moved = sorted(
            key
            for key in read_row
            if key not in named
            and key != "university"
            and body_row.get(key) != read_row.get(key)
        )
        if moved:
            raise WriteRefused(
                "Refusing to send an education row whose "
                + ", ".join(moved)
                + " differs from the read without having been asked for. The body is "
                "the row the server returned with only the named fields replaced; a "
                "value that moved on its own is a bug in the copy, not a change worth "
                "sending.",
                fields=moved,
            )

    def _education_body_row(self, read_row: dict, supplied: dict) -> dict:
        """Copy the row, collapse the university, replace only what was named."""
        validated = {
            k: self._validate_education_value(k, v) for k, v in supplied.items()
        }
        body_row = dict(read_row)
        if "university" in body_row:
            body_row["university"] = self._collapse_university(body_row["university"])
        body_row.update(validated)
        self._guard_education_keys(read_row, body_row)
        self._guard_education_untouched(read_row, body_row, set(validated))
        return body_row

    def _guard_removal_halves(
        self, rows: list[dict], objects: list[dict], deleted: list
    ) -> None:
        """Both halves or neither, asserted on the payload rather than in a test.

        The site does TWO things to a removed row inside one handler: pushes
        its resource_uri onto the deleted list and splices it out of the rows
        it is about to send. A row that appears in ``deleted_objects`` while
        still riding ``objects`` is a request the site has never made, and on
        a resource whose removal semantics are UNMEASURED that is exactly the
        payload whose answer nobody can predict.

        The mirror is checked in the same breath and is the more dangerous of
        the two: a row that quietly left ``objects`` WITHOUT being named for
        removal would be a deletion nobody asked for under the omission
        reading, and a survivor that ignored the request under the other.
        Neither is a request this server is willing to send.
        """
        gone = {u for u in deleted if isinstance(u, str)}
        riding = [r.get("resource_uri") for r in objects]
        both = sorted(
            str(r.get("id")) for r in objects if r.get("resource_uri") in gone
        )
        if both:
            raise WriteRefused(
                "Refusing a payload that names education row(s) "
                + ", ".join(both)
                + " in "
                + C.EDUCATION_DELETED_OBJECTS_KEY
                + " while still sending "
                + ("it" if len(both) == 1 else "them")
                + " in objects. A removal is a push AND a splice; half of one is "
                "a request the site has never sent.",
                fields=both,
            )
        missing = sorted(
            str(r.get("id"))
            for r in rows
            if r.get("resource_uri") not in gone
            and r.get("resource_uri") not in riding
        )
        if missing:
            raise WriteRefused(
                "Refusing a payload that drops education row(s) "
                + ", ".join(missing)
                + " from objects without naming "
                + ("it" if len(missing) == 1 else "them")
                + " in "
                + C.EDUCATION_DELETED_OBJECTS_KEY
                + ". Whether an omitted row is deleted by this resource is NOT "
                "measured, so a row that leaves silently is either a deletion "
                "nobody asked for or a removal that will not happen -- and which "
                "one is unknowable from here.",
                fields=missing,
            )

    @staticmethod
    def _same_graduation_year(before: Any, after: Any) -> bool:
        """2019 and "2019" are the same year, and one is not a change to make.

        Without this the int the server returns and the string the wire wants
        would never compare equal, "already at that value" could never fire, and
        asking for the year already on the profile would send a live write that
        changes nothing but the serialization.
        """
        if before is None or after is None:
            return before is after
        return str(before).strip() == str(after).strip()

    def _find_education_row(self, rows: list[dict], education_id: Any) -> dict:
        if isinstance(education_id, bool) or not isinstance(education_id, int):
            raise InvalidFilter(
                "education_id must be the integer id of an existing education row. "
                "Read them from instahyre_get_profile or the preview of this tool.",
                field="education_id",
            )
        for row in rows:
            if row.get("id") == education_id:
                return row
        raise InvalidFilter(
            f"No education row with id {education_id}. The rows on this profile are: "
            + ", ".join(str(r.get("id")) for r in rows)
            + ". A row is addressed by its own id, never by position -- an index would "
            "silently move when a row is added.",
            field="education_id",
        )

    def _describe_education(self, row: dict) -> dict:
        """The fields a caller reasons about, resolved through both spellings."""
        university = row.get("university")
        if isinstance(university, dict):
            institute = university.get("name") or university.get("resource_uri")
        else:
            institute = university
        degree = row.get("current_degree")
        return {
            "id": row.get("id"),
            "institute": institute,
            "degree": degree.get("name") if isinstance(degree, dict) else degree,
            "graduation_year": row.get("graduation_year"),
            "gpa": row.get("gpa"),
            "grading_scale": row.get("grading_scale"),
        }

    def plan_education(
        self, education_id: Any, *, remove: bool = False, **changes: Any
    ) -> dict:
        """What an education write would send, without sending it. Reads only.

        ``remove`` builds the OTHER request this resource answers. It is
        keyword-only and deliberately outside ``**changes``: a field named
        "remove" would otherwise be indistinguishable from the flag, and this
        is the one argument on this method that deletes something.
        """
        supplied = {k: v for k, v in changes.items() if v is not None}
        if not supplied and not remove:
            raise InvalidFilter(
                "Nothing to update -- pass at least one of: "
                + ", ".join(C.EDUCATION_WRITABLE_FIELDS),
                field="fields",
            )

        related = sorted(k for k in supplied if k in C.EDUCATION_RELATED_FIELDS)
        if related:
            raise WriteRefused(
                "Not writable through this server: "
                + ", ".join(related)
                + ". "
                + "; ".join(
                    "%s is %s" % (k, C.EDUCATION_RELATED_FIELDS[k]) for k in related
                )
                + ". Changing one of these means resolving a row out of a taxonomy the "
                "page had already loaded -- and for the institute, an autocomplete that "
                "can CREATE a row -- which is a wider contract than the one that was "
                "captured and which nobody has measured. They are echoed back untouched "
                "on every write. Change them at "
                + C.SITE_BASE
                + "/candidate/profile/ where the taxonomy is on screen.",
                fields=related,
            )

        unknown = sorted(k for k in supplied if k not in C.EDUCATION_WRITABLE_FIELDS)
        if unknown:
            raise WriteRefused(
                "Not writable through this server: "
                + ", ".join(unknown)
                + ". Writable education fields are: "
                + ", ".join(C.EDUCATION_WRITABLE_FIELDS)
                + ".",
                fields=unknown,
            )

        rows = self.read_education()
        return self._education_plan_from(rows, education_id, supplied, remove=remove)

    def _education_plan_from(
        self,
        rows: list[dict],
        education_id: Any,
        supplied: dict,
        *,
        remove: bool = False,
    ) -> dict:
        # One request edits a row or removes it, never both. There is no
        # coherent serialization of "set the year on row 7 and delete row 7":
        # the edited row would ride ``objects`` while its uri rode
        # ``deleted_objects``, which is the one combination the site never
        # sends and precisely what _guard_removal_halves refuses. Same call
        # plan_skills makes on "add X and remove X" -- refused rather than
        # resolved, because either resolution guesses which word was meant.
        #
        # Both public doors pass one or the other, so this is a BACKSTOP for a
        # direct call, in the same class as the final raise in
        # _validate_education_value. It is kept because the two doors are the
        # only thing making it unreachable, and a third one is an edit away.
        if remove and supplied:
            raise WriteRefused(
                "Refusing a request that both edits and removes education row "
                + str(education_id)
                + ": "
                + ", ".join(sorted(supplied))
                + " would be written onto a row the same request deletes. An "
                "edited row rides objects and a removed one is named in "
                + C.EDUCATION_DELETED_OBJECTS_KEY
                + "; a row in both is a payload the site has never sent and whose "
                "answer nobody has measured. Ask for one or the other.",
                fields=sorted(supplied),
            )

        target = self._find_education_row(rows, education_id)

        # A reverse marketplace finds him BY the filters on this row --
        # employers filter on degree and institute -- so an empty education
        # section is not a shorter profile, it is one that drops out of every
        # filtered result set it would otherwise have appeared in. Same
        # reasoning and same shape as update_skills' refusal to empty the
        # skill list, with one deliberate difference: this fires in the PLAN,
        # so confirm=False refuses too. A skills request that empties the list
        # may still be ADDING, so its preview has something to show; a
        # last-row removal request contains nothing else, and previewing a
        # payload that can never be sent would be the tool implying it might.
        if remove and len(rows) <= 1:
            raise WriteRefused(
                "Refusing to remove the only education row on the profile. "
                "Instahyre is a reverse marketplace: employers filter on degree "
                "and institute, so a profile with no education is not a shorter "
                "profile but one that drops out of the filtered result sets it "
                "would otherwise appear in -- this would suppress future match "
                "cycles rather than one application. Refused outright rather than "
                "confirm-gated: confirm=True does not reach this either. If it is "
                "really wanted, do it on the website at "
                + C.SITE_BASE
                + "/candidate/profile/ where the consequence is visible.",
                rows_on_profile=len(rows),
                education_id=education_id,
            )

        # The removal channel is a list of RESOURCE URIS -- removeEmptyRow
        # pushes education.resource_uri and nothing else. There is no id-shaped
        # spelling to fall back on, so a row without one is reported rather
        # than written around.
        uri = target.get("resource_uri") if remove else None
        if remove and not (isinstance(uri, str) and uri):
            raise WriteRefused(
                "Education row "
                + str(education_id)
                + " carries no resource_uri, and "
                + C.EDUCATION_DELETED_OBJECTS_KEY
                + " is a list of resource URIs -- that is the only spelling the "
                "site's own removeEmptyRow pushes. Inventing an id-shaped one "
                "would be the guessed body this package exists to refuse.",
                education_id=education_id,
            )

        absent = sorted(k for k in supplied if k not in target)
        if absent:
            raise WriteRefused(
                "The education row the server returned does not carry "
                + ", ".join(absent)
                + ", so setting "
                + ("it" if len(absent) == 1 else "them")
                + " would mean adding a key the read did not return -- a body the "
                "platform has never been sent. The captured row carried all ten keys "
                "including gpa and grading_scale, and no shipped bundle names either "
                "of them, so they are the server's to send. A row that arrives without "
                "them is a different world from the one that was measured: report it "
                "rather than writing into it.",
                fields=absent,
            )

        objects = []
        target_index = None
        for row in rows:
            if row is target:
                # ``is``, never ``==``, and never list.index -- two education
                # rows that happen to hold equal values would make an
                # equality-based lookup edit whichever came first.
                if remove:
                    # THE SPLICE, which is the half of a removal that is easy
                    # to forget. removeEmptyRow pushes the uri onto the deleted
                    # list AND splices the row out of $scope.educations, in one
                    # handler, before one save. A payload that did only the push
                    # would send a row the same request asks to delete.
                    continue
                target_index = len(objects)
                objects.append(self._education_body_row(row, supplied))
            else:
                # UNTOUCHED rows still get the university collapse, because the
                # transformation is what the resource is sent, not an edit. What
                # they never get is a substitution.
                objects.append(self._education_body_row(row, {}))

        deleted = [uri] if remove else []
        self._guard_removal_halves(rows, objects, deleted)

        # ``supplied`` is empty on a removal -- the gate at the top of this
        # method makes sure of it -- so the comparison loop below is a no-op
        # there and ``changed`` stays empty. ``target`` stands in for a body
        # row that no longer exists, only so row_key_count can report what the
        # row that leaves was carrying.
        body_row = target if remove else objects[target_index]
        changed = {}
        for key in supplied:
            before, after = target.get(key), body_row[key]
            if key == "graduation_year" and self._same_graduation_year(before, after):
                continue
            if before != after:
                changed[key] = {"from": before, "to": after}
        unchanged = sorted(k for k in supplied if k not in changed)

        collapsed = sorted(
            row.get("id")
            for row in rows
            if isinstance(row.get("university"), dict)
        )
        plan = {
            "would_send": {
                "method": C.EDUCATION_PATCH_METHOD,
                "url": C.API_BASE + C.EP_EDUCATION,
                "json_body": {
                    "objects": objects,
                    C.EDUCATION_DELETED_OBJECTS_KEY: deleted,
                },
                "headers": {
                    "Content-Type": "application/json",
                    C.APPLY_CSRF_HEADER: "<from the csrftoken cookie>",
                },
            },
            "education_id": target.get("id"),
            # Where the edited row sits in ``objects``. Published so the write
            # can read its own payload back without re-finding the row by id --
            # objects[0] is the edited row only when it happens to be first.
            "target_row_index": target_index,
            "would_change": changed,
            "already_at_that_value": unchanged,
            "rows_on_profile": len(rows),
            "rows_in_body": len(objects),
            "row_key_count": len(body_row),
            "reads_as_now": self._describe_education(target),
            "would_read_as": (
                None if remove else self._describe_education(body_row)
            ),
            "other_rows_ride_unchanged": [
                r.get("id") for r in rows if r is not target
            ],
            "university_collapsed_on_rows": collapsed,
            "every_row_rides": (
                (
                    "%d of the %d rows the read returned are in this payload. The "
                    "one that is not is the row being REMOVED, and it is named in "
                    "%s by its resource_uri -- both halves of the site's own "
                    "removeEmptyRow, in one request. Every other row still rides "
                    "verbatim, which is what keeps this a removal of exactly one "
                    "row rather than a rewrite of the section."
                    % (len(objects), len(rows), C.EDUCATION_DELETED_OBJECTS_KEY)
                )
                if remove
                else (
                    "All %d rows the read returned are in this payload, not just the "
                    "one being edited. Whether an omitted ROW is deleted by this "
                    "resource is NOT measured -- the sibling multi_save resource does "
                    "delete omitted rows, and education additionally has its own "
                    "deleted_objects channel, so the two readings disagree. Sending "
                    "every row is correct under both."
                    % len(rows)
                )
            ),
            "one_transformation": (
                "university is sent as a resource URI, collapsed from the expanded "
                "object the GET returns. That is the site's own constructEducationObj, "
                "and it is the ONLY value this server changes that it was not asked to "
                "change. current_degree stays EXPANDED beside the degree URI, exactly "
                "as the captured request carried it."
            ),
        }
        if remove:
            plan["would_remove"] = {
                "id": target.get("id"),
                "resource_uri": uri,
                "reads_as": self._describe_education(target),
                "how": (
                    "BOTH HALVES, in one PATCH. The uri above is named in "
                    + C.EDUCATION_DELETED_OBJECTS_KEY
                    + " and the row is absent from objects, which is what "
                    "removeEmptyRow does in the site's own code -- push, then "
                    "splice, then save. No DELETE verb exists on this resource."
                ),
                "restorable": (
                    "NO, not cleanly, and this is the honest answer rather than "
                    "the reassuring one. A snapshot IS written before the request "
                    "and it holds this row whole -- but restore_education REFUSES "
                    "to send a row whose id the server no longer has, because "
                    "whether this resource re-creates it, ignores it, or rejects "
                    "the whole payload is NOT measured, and guessing would risk "
                    "the rows that are still there. Even if that refusal were "
                    "lifted, a re-added row gets a NEW id and a NEW resource_uri, "
                    "so what came back would be a different row wearing the same "
                    "values. Re-add the entry at "
                    + C.SITE_BASE
                    + "/candidate/profile/ and copy the values out of the "
                    "snapshot by hand."
                ),
            }
            plan["deleted_objects_carries"] = list(deleted)
        else:
            plan["deleted_objects_is_empty"] = (
                "On an EDIT, always. The channel's shape is known from the site's "
                "own removeEmptyRow -- a list of resource URIs -- and nothing but "
                "a removal request fills it. An edit that named a row here would "
                "be asking to delete the row it is editing."
            )
        return plan

    def update_education(
        self,
        education_id: Any,
        *,
        graduation_year: Any = None,
        gpa: Optional[float] = None,
        grading_scale: Optional[float] = None,
        remove: bool = False,
        confirm: bool = False,
    ) -> dict:
        """Write one education row. One PATCH, carrying every row.

        On a reverse marketplace degree, institute and graduation year are
        filters employers search on, so this is the same class of leverage as
        the job-search profile rather than a cosmetic detail.

        ``remove`` REMOVES the row instead of editing it, and it is the one
        argument here that destroys something. The request is the same PATCH
        with the same envelope: the row is spliced out of ``objects`` and its
        resource_uri is named in ``deleted_objects``, which is exactly what
        the site's own removeEmptyRow does -- push, then splice, then save.

        THE ELEMENT SHAPE IS READ FROM SHIPPED SOURCE, NOT FROM THE WIRE, and
        the difference is worth holding on to. The envelope was captured; the
        capture caught ``deleted_objects`` EMPTY, so no removal has ever been
        serialized or answered. What settles the element is the page's own
        handler, which pushes ``education.resource_uri`` and nothing else --
        so the list is of resource URI strings, not ids and not objects. That
        is why the removal verifies itself by re-reading rather than trusting
        a 200, and why _guard_removal_halves asserts the payload's two halves
        agree before anything is sent.

        A REMOVAL IS NOT CLEANLY UNDOABLE, and saying otherwise would be the
        comfortable lie. A snapshot is written first and holds the row whole,
        but :meth:`restore_education` REFUSES to send a row whose id the
        server no longer has -- whether this resource re-creates it, ignores
        it or rejects the whole payload is not measured, and guessing would
        risk the rows still standing. Even with that refusal lifted, a
        re-added row gets a NEW id and a NEW resource_uri: the values can be
        copied back by hand from the snapshot, the row itself cannot.

        Two MCP tools sit on this one method, on purpose. The verb that
        deletes gets its own name and its own docstring where a caller reads
        it, and ``instahyre_update_education`` keeps -- structurally, not by a
        default -- the property that no argument of its own can fill the
        removal channel.
        """
        supplied_raw = {
            "graduation_year": graduation_year,
            "gpa": gpa,
            "grading_scale": grading_scale,
        }
        plan = self.plan_education(education_id, remove=remove, **supplied_raw)
        if not confirm:
            plan["executed"] = False
            if remove:
                plan["next_step"] = (
                    "NOTHING HAS BEEN SENT. Call again with confirm=True to remove "
                    "education row "
                    + str(education_id)
                    + ". Read would_remove first, and its restorable key most of "
                    "all: that row is DELETED by this write and does not come back "
                    "the way a changed field does. A snapshot is taken "
                    "automatically, and it is not an undo."
                )
            else:
                plan["next_step"] = (
                    "Nothing has been sent. Call again with confirm=True to write. A "
                    "snapshot is taken automatically first, and instahyre_restore_profile "
                    "with scope='education' puts every row back."
                )
            return plan

        # A removal changes no FIELD, so would_change is empty by construction
        # and this no-op refusal would fire on every removal if it were not
        # scoped to the edit path. The removal has its own way of being a
        # no-op -- a row that is not there -- and _find_education_row already
        # refuses that by name, before a snapshot or a request.
        if not remove and not plan["would_change"]:
            raise WriteRefused(
                "Nothing to write: every field supplied already holds that value. "
                "Refusing to send a request that cannot change anything -- a no-op "
                "write is indistinguishable from a broken one, and this one would "
                "still send every education row to achieve it."
            )

        if not self.http.cookies.get("csrftoken"):
            raise WriteRefused(
                "Refusing to write without a CSRF token -- Django would reject the "
                "request and the result would be ambiguous. Run instahyre_auth_status."
            )

        # The snapshot's rows ARE the rows the body is built from, so the restore
        # point and the payload describe the same instant. Re-reading here would
        # open a window in which the two could disagree.
        rows = self.read_education()
        record, snap = self.take_snapshot(label="pre-education-write", education=rows)
        captured = record.get("education")
        if not isinstance(captured, list) or not captured:
            raise WriteRefused(
                "The snapshot captured no education rows, so there would be no restore "
                "point for a write that sends every row. Refusing to write without one."
            )

        supplied = {k: v for k, v in supplied_raw.items() if v is not None}
        final = self._education_plan_from(
            captured, education_id, supplied, remove=remove
        )
        if not remove and not final["would_change"]:
            raise WriteRefused(
                "Between the preview and the write the row already moved to the "
                "requested values. Nothing was sent."
            )
        body = final["would_send"]["json_body"]
        before_rows = {r.get("id"): r for r in captured}

        if remove:
            log.warning(
                "REMOVING education row %s from the live profile; %d rows remain",
                education_id,
                len(body["objects"]),
            )
        else:
            log.warning(
                "writing education row %s on the live profile: %s",
                education_id,
                sorted(final["would_change"]),
            )
        self.http.patch(C.EP_EDUCATION, json_body=body)

        after_rows = self.read_education()
        after = {r.get("id"): r for r in after_rows}
        target_after = after.get(education_id) or {}
        # None on a removal: the row was spliced out, so no sent row exists to
        # compare against. ``would_change`` is empty there too, which is what
        # makes the comprehension below a no-op rather than a lookup into {}.
        sent_row = (
            {}
            if final["target_row_index"] is None
            else body["objects"][final["target_row_index"]]
        )
        mismatched = {
            key: {"wanted": sent_row.get(key), "got": target_after.get(key)}
            for key in final["would_change"]
            if not self._education_field_landed(key, final, target_after)
        }
        # The collateral report. Every row rides this write, so every row is a
        # place the server could have moved something nobody named -- and a
        # report that covered only the edited row would hide exactly that.
        # Keys are stringified because these dicts are serialized to JSON on the
        # way to a caller, where an integer key is not a key.
        also_changed: dict = {}
        for row_id, before in before_rows.items():
            if remove and row_id == education_id:
                # The row this request removed. Its absence is the OUTCOME and
                # is checked by name below; reporting it here would make every
                # successful removal read as collateral damage.
                continue
            now = after.get(row_id)
            if now is None:
                also_changed[str(row_id)] = {"before": "present", "after": "GONE"}
                continue
            moved = {
                key: {"before": before.get(key), "after": now.get(key)}
                for key in sorted(set(before) | set(now))
                if not (row_id == education_id and key in final["would_change"])
                and not self._education_values_agree(key, before.get(key), now.get(key))
            }
            if moved:
                also_changed[str(row_id)] = moved
        appeared = sorted(str(k) for k in after if k not in before_rows)

        # The removal's own outcome, and the ONLY thing that makes a removal
        # verified. A 200 with the row still on the profile is exactly the
        # silent no-op this package refuses to call success -- and on this
        # resource it is the likelier failure of the two, because no removal
        # has ever been answered and the server may simply ignore the channel.
        row_is_gone = (education_id not in after) if remove else None
        result = {
            "executed": True,
            "education_id": education_id,
            "updated": sorted(final["would_change"]),
            "changed": final["would_change"],
            "snapshot_id": snap["snapshot_id"],
            "verified": (
                (bool(row_is_gone) and not also_changed and not appeared)
                if remove
                else (not mismatched and not also_changed and not appeared)
            ),
            "mismatched": mismatched or None,
            "verified_by": "re-read of GET " + C.EP_EDUCATION + " after the write",
            "reads_as_now": (
                None if remove else self._describe_education(target_after)
            ),
            "rows_sent": len(body["objects"]),
            "rows_now": len(after_rows),
            "also_changed_by_the_server": also_changed or None,
            "rows_that_appeared": appeared or None,
        }
        if remove:
            result["removed"] = final["would_remove"]["reads_as"]
            result["removed_row_id"] = education_id
            result["deleted_objects_sent"] = body[C.EDUCATION_DELETED_OBJECTS_KEY]
            result["row_is_gone"] = row_is_gone
            result["rows_that_remain"] = [
                self._describe_education(r) for r in after_rows
            ]
            result["restorable"] = final["would_remove"]["restorable"]
            if not row_is_gone:
                result["warning"] = (
                    "THE REMOVAL DID NOT TAKE. The request was accepted and the row "
                    "is still on the profile when read back. That is the branch "
                    "nobody had measured -- it says this resource does not act on "
                    + C.EDUCATION_DELETED_OBJECTS_KEY
                    + " the way its own client implies, which is a FINDING and "
                    "should be recorded before anything is retried. Nothing further "
                    "has been attempted. The other rows were sent verbatim, so the "
                    "profile should be unchanged; the snapshot above is how to check."
                )
                log.error(
                    "education removal did not take: row %s is still on the profile",
                    education_id,
                )
        if also_changed or appeared:
            result["collateral_note"] = (
                "These rows or keys were not requested and moved anyway. On this "
                "resource that is a FINDING rather than an expected recomputation: "
                "unlike the job-search profile, no key here is known to be derived "
                "from another. A row reported GONE means omission-is-deletion is real "
                "on this resource after all, which would be the measurement nobody has "
                "made -- record it. The snapshot above is how to put it back."
            )
        if mismatched:
            result["warning"] = (
                "THE WRITE DID NOT VERIFY. The request was accepted but the row does "
                "not read back as intended. Nothing further has been attempted -- call "
                "instahyre_restore_profile with scope='education' and the snapshot_id "
                "above, and do not retry until the difference is understood."
            )
            log.error("education write did not verify: %s", sorted(mismatched))
        return result

    def _education_values_agree(self, key: str, wanted: Any, got: Any) -> bool:
        """Equality, with the two representation differences this resource has.

        Both are named rather than absorbed into a general "coerce both sides"
        comparison, which would make a genuinely changed id look equal to its
        own string -- the opposite of what a verify step is for.

        ``graduation_year`` travels as a string and may be read back as an
        integer. ``university`` travels as a resource URI and the GET returns it
        EXPANDED, so the write's own payload and the read that verifies it are
        in different representations BY DESIGN. Which spelling the server echoes
        immediately after a PATCH is not measured, so both are treated as the
        same value when they name the same row -- and a DIFFERENT institute
        still compares unequal, which is the case this has to keep catching.
        """
        if key == "graduation_year":
            return self._same_graduation_year(wanted, got)
        if key == "university":
            return self._collapse_university(wanted) == self._collapse_university(got)
        return wanted == got

    def _education_field_landed(self, key: str, plan: dict, after: dict) -> bool:
        """Did the field actually take, allowing for the server's own typing.

        graduation_year is sent as a string and may well be READ back as an
        integer, which is exactly the difference this write does not consider a
        failure. Comparing raw would report every successful year change as
        unverified, which is the false alarm that gets a verify step deleted.
        """
        wanted = plan["would_change"][key]["to"]
        return self._education_values_agree(key, wanted, after.get(key))

    def restore_education(
        self, snapshot_id: Optional[str] = None, *, confirm: bool = False
    ) -> dict:
        """Put every education row back to a snapshot. One PATCH does it.

        Restoring is the same request as writing, with older contents -- the
        snapshot's rows are a valid body once the university collapse is applied,
        which is the same transformation the write performs.

        A row that has been DELETED since the snapshot is a case this refuses
        rather than guesses at. The snapshot's row would carry the id and
        resource_uri of a row the server no longer has, and whether this resource
        tolerates that, creates a new row, or rejects the whole payload is not
        measured -- unlike skills, where the answer was learned by watching one
        disappear. Guessing here could take the surviving rows down with it.
        """
        snap = self.load_snapshot(snapshot_id)
        target = snap.get("education")
        if not isinstance(target, list) or not target:
            raise WriteRefused(
                "That snapshot holds no education rows. Only a snapshot taken by an "
                "education write captures them -- every other write path deliberately "
                "does not fetch the collection, so a skills or job-search-profile "
                "snapshot has nothing to restore here. Use "
                "instahyre_list_profile_snapshots and pick one with education_captured "
                "set.",
                snapshot=str(snap.get("snapshot_id")),
            )

        current = self.read_education()
        current_by_id = {r.get("id"): r for r in current}
        snapshot_ids = [r.get("id") for r in target]
        vanished = sorted(str(i) for i in snapshot_ids if i not in current_by_id)
        if vanished:
            raise WriteRefused(
                "Education row(s) " + ", ".join(vanished) + " are in the snapshot but "
                "not on the profile any more. Restoring would send a row whose id the "
                "server no longer has, and whether this resource re-creates it, ignores "
                "it or rejects the whole payload is NOT measured. Refusing rather than "
                "risking the rows that are still there. Re-add the entry at "
                + C.SITE_BASE
                + "/candidate/profile/ and restore the values by hand.",
                fields=vanished,
            )
        extra = sorted(str(i) for i in current_by_id if i not in snapshot_ids)

        objects = [self._education_body_row(row, {}) for row in target]
        differs: dict = {}
        for row in target:
            row_id = row.get("id")
            now = current_by_id.get(row_id, {})
            moved = {
                key: {"now": now.get(key), "snapshot": row.get(key)}
                for key in sorted(set(row) | set(now))
                if not self._education_values_agree(key, row.get(key), now.get(key))
            }
            if moved:
                differs[str(row_id)] = moved

        preview = {
            "snapshot_id": snap.get("snapshot_id"),
            "taken_at": snap.get("taken_at"),
            "would_send": {
                "method": C.EDUCATION_PATCH_METHOD,
                "url": C.API_BASE + C.EP_EDUCATION,
                "json_body": {
                    "objects": objects,
                    C.EDUCATION_DELETED_OBJECTS_KEY: [],
                },
            },
            "would_revert": differs,
            "rows_in_snapshot": len(target),
            "rows_on_profile": len(current),
            "rows_added_since_the_snapshot": extra,
            "reads_as_now": [self._describe_education(r) for r in current],
            "would_read_as": [self._describe_education(r) for r in target],
        }
        if extra:
            preview["rows_added_note"] = (
                "These rows were added after the snapshot and are NOT in this payload. "
                "Whether this resource deletes a row omitted from objects is not "
                "measured, so they may survive or may not -- which is why they are "
                "named here before anything is sent."
            )
        if not confirm:
            preview["executed"] = False
            preview["next_step"] = "Call again with confirm=True to restore."
            return preview
        if not differs:
            raise WriteRefused(
                "The live education rows already match that snapshot. Nothing to "
                "restore, and nothing was sent."
            )
        if not self.http.cookies.get("csrftoken"):
            raise WriteRefused(
                "Refusing to write without a CSRF token. Run instahyre_auth_status."
            )

        self.take_snapshot(label="pre-education-restore", education=current)
        log.warning("restoring education from %s", snap.get("snapshot_id"))
        self.http.patch(
            C.EP_EDUCATION,
            json_body={"objects": objects, C.EDUCATION_DELETED_OBJECTS_KEY: []},
        )

        after = {r.get("id"): r for r in self.read_education()}
        still_off: dict = {}
        for row in target:
            row_id = row.get("id")
            now = after.get(row_id, {})
            off = {
                key: {"wanted": row.get(key), "got": now.get(key)}
                for key in sorted(set(row) | set(now))
                if not self._education_values_agree(key, row.get(key), now.get(key))
            }
            if off:
                still_off[str(row_id)] = off
        return {
            "executed": True,
            "snapshot_id": snap.get("snapshot_id"),
            "reverted": sorted(differs),
            "verified": not still_off,
            "still_differs": still_off or None,
            "verified_by": "re-read of GET " + C.EP_EDUCATION + " after the restore",
            "reads_as_now": [self._describe_education(r) for r in after.values()],
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
