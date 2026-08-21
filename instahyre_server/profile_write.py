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

**The write is add-only, and that is now load-bearing rather than merely
cautious.** It was originally unknown whether the server treats
``{objects: [...]}`` as a full replacement set or as tastypie's ordinary
additive ``patch_list``, so the write was shaped to give the same result under
either. It has since been MEASURED, by adding one canary skill and then sending
a payload that omitted it: the row was removed. **The resource is a full
replacement set.** Anything absent from ``objects`` is deleted.

That turns the add-only rule from a hedge into the thing standing between a
partial list and a wiped profile. Every write echoes each existing skill back
**verbatim as the server returned it**, so the payload always says "these are
all of them" truthfully.

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

#: Fields a caller will reasonably ask for that live on the job-search-profile
#: sub-object, not the candidate. Writing one means PUTting the WHOLE jsp back,
#: which is a different and much wider contract. Refused by name, with the
#: reason, rather than silently ignored or guessed at.
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
        skills = self.read_skills()
        raw = self.inbound.http.get(
            C.EP_PROFILE.format(candidate_id=self.candidate_id())
        )
        scalars = {k: raw.get(k) for k in WRITABLE_SCALARS if k in raw}

        record = {
            "snapshot_id": f"{int(time.time())}-{label}",
            "taken_at": time.time(),
            "label": label,
            "candidate_skills": skills,
            "scalars": scalars,
            "skill_names": [s.get("name") for s in skills],
        }
        path = snapshots_dir() / f"{record['snapshot_id']}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        log.info("profile snapshot written: %s (%d skills)", path.name, len(skills))
        return {
            "snapshot_id": record["snapshot_id"],
            "path": str(path),
            "skills_captured": len(skills),
            "skill_names": record["skill_names"],
            "scalars_captured": sorted(scalars),
        }

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
            raise WriteRefused(
                f"Snapshot {path.name} could not be read as JSON ({exc}). Refusing to "
                "restore from a file this server cannot understand -- a restore deletes "
                "every skill the snapshot does not mention."
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

    def plan_skills(self, add: list[str]) -> dict:
        """Work out exactly what would be sent, and refuse what should not be.

        Runs the full validation path, so a preview that comes back clean means
        the write itself will not fail validation.
        """
        current = self.read_skills()
        current_names = [s.get("name", "") for s in current if s.get("name")]
        lowered = {n.strip().lower() for n in current_names}

        requested = [str(s).strip() for s in (add or [])]
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

        total = len(current_names) + len(additions)
        over_by = max(0, total - C.MAX_SKILLS)
        dropped_for_cap: list[str] = []
        if over_by:
            dropped_for_cap = additions[len(additions) - over_by :]
            additions = additions[: len(additions) - over_by]

        uri = self.candidate_uri()
        objects = list(current) + [{"candidate": uri, "name": n} for n in additions]

        return {
            "current_skills": current_names,
            "would_add": additions,
            "skipped_already_on_profile": already_on_profile,
            "skipped_repeated_in_request": repeated_in_request,
            "rejected": rejected,
            "dropped_over_platform_cap": dropped_for_cap,
            "resulting_skill_count": len(current_names) + len(additions),
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
            "add_only": (
                "Existing rows are echoed back byte-for-byte as the server returned "
                "them. This matters: the resource is a FULL REPLACEMENT SET -- measured, "
                "by omitting one row and watching it be deleted -- so a payload that "
                "left any current skill out would remove it. Add-only is what makes the "
                "payload's implicit claim ('these are all of them') true."
            ),
            "not_sent": (
                "The site also PUTs the whole job-search-profile object afterwards. This "
                "server does not: that request carries no skills, and it NULLs two "
                "career-break fields on the way past."
            ),
        }

    def update_skills(self, add: list[str], *, confirm: bool = False) -> dict:
        """Add skills to the live profile. Snapshots first, verifies after."""
        plan = self.plan_skills(add)

        if not confirm:
            plan["executed"] = False
            plan["next_step"] = (
                "Nothing has been sent. Call again with confirm=True to write. A "
                "snapshot is taken automatically first, and instahyre_restore_profile "
                "puts it back."
            )
            return plan

        if not plan["would_add"]:
            raise WriteRefused(
                "Nothing to write: every requested skill is already on the profile, was "
                "rejected, or did not fit under the platform's "
                f"{C.MAX_SKILLS}-skill cap. Refusing to send a request that cannot "
                "change anything -- a no-op write is indistinguishable from a broken one."
            )

        if not self.http.cookies.get("csrftoken"):
            raise WriteRefused(
                "Refusing to write without a CSRF token -- Django would reject the "
                "request and the result would be ambiguous. Run instahyre_auth_status."
            )

        snap = self.snapshot(label="pre-skills-write")
        body = plan["would_send"]["json_body"]

        log.warning("writing %d new skills to the live profile", len(plan["would_add"]))
        response = self.http.patch(C.EP_SKILL_MODEL, json_body=body)

        verification = self._verify_skills(
            expected=set(n.lower() for n in plan["current_skills"] + plan["would_add"])
        )
        result = {
            "executed": True,
            "added": plan["would_add"],
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

        The consequence for callers is the important part, and it is why
        :meth:`update_skills` is add-only: any partial list sent to this
        resource DELETES everything absent from it.
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

        if not confirm:
            return {
                "executed": False,
                "snapshot_id": snap.get("snapshot_id"),
                "current_skills": current_names,
                "would_restore_to": target_names,
                "would_drop": will_drop,
                "how": (
                    "One PATCH carrying the snapshot's rows. The resource is a full "
                    "replacement set, so anything absent from that list is removed by "
                    "the same request."
                ),
                "next_step": "Call again with confirm=True to restore.",
            }

        self.snapshot(label="pre-restore")
        self.http.patch(C.EP_SKILL_MODEL, json_body={"objects": target})

        after = self.skill_names()
        ok = {n.lower() for n in after} == {n.lower() for n in target_names if n}
        return {
            "executed": True,
            "snapshot_id": snap.get("snapshot_id"),
            "restored_to": target_names,
            "skills_now": after,
            "dropped": will_drop,
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

    # -- scalar fields -----------------------------------------------------

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
            raise WriteRefused(
                "Refusing to write "
                + ", ".join(sorted(refused.values()))
                + ": those live on the job-search-profile sub-object, not the candidate "
                "record, so setting one means PUTting that whole object back. That "
                "contract is not verified here, and a partly-correct PUT would blank "
                "neighbouring fields. Change these at "
                + C.SITE_BASE
                + "/candidate/profile/ instead.",
                fields=sorted(refused),
            )

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
