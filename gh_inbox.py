#!/usr/bin/env python3
"""gh-inbox: send a macOS alert only for GitHub activity that needs you.

An alert fires when someone:
- requests your review directly (not through a team),
- mentions you in a comment, a review or a new PR or issue description,
- replies to your review comment, or assigns you,
- reviews, comments on or merges your PR or issue,
- comments on a PR or issue that you commented on or reviewed.
An alert also fires when a workflow run that you started fails,
or when a deployment waits for your approval.

Bots and your own actions never fire an alert. Activity that you already read on GitHub is skipped.
Draft PRs fire no alert (skip_drafts), and their notifications can be marked as done (done_drafts).
The script gets the GitHub token through the `gh` CLI. It stores no credentials.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

STATE_DIR = Path.home() / ".local/state/gh-inbox"
STATE_FILE = STATE_DIR / "state.json"
CONFIG_FILE = Path.home() / ".config/gh-inbox/config.json"

DEFAULT_CONFIG = {
    "ignore_repos": [],  # fnmatch patterns, for example "some-org/*"
    "ignore_users": [],  # human logins to treat like bots
    "include_bots": False,
    "team_review_requests": False,
    "participant_comments": True,  # new comments on PRs and issues that you commented on or reviewed
    "skip_drafts": True,  # no alert for draft PRs
    "done_drafts": False,  # also mark unread draft PR notifications as done on GitHub
    "ci_failures": True,
    "deploy_approvals": True,
    "max_alerts": 5,  # per run; the rest collapse into one "N more" alert
    "sound": "default",
}

GH = shutil.which("gh")
NOTIFIER = shutil.which("terminal-notifier")
OVERLAP = timedelta(minutes=5)  # re-read a short window so that late API writes are not lost
SEEN_TTL = timedelta(days=14)
PARTICIPANT_REASONS = {"comment", "mention", "manual", "assign"}
SKIP_REASONS = {"subscribed", "state_change"}
SUBJECT_RE = re.compile(r"/repos/([^/]+)/([^/]+)/(?:pulls|issues)/(\d+)$")
# Most important first. Each actor gets the label of their most important event.
LABELS = [
    "requested your review",
    "requested changes",
    "assigned you",
    "mentioned you",
    "replied to you",
    "approved",
    "requested your team's review",
    "merged it",
    "reviewed",
    "commented",
]

QUERY = """
query($owner: String!, $name: String!, $number: Int!, $since: DateTime!) {
  repository(owner: $owner, name: $name) {
    issueOrPullRequest(number: $number) {
      ... on Issue {
        id title url createdAt body author { ...Actor }
        comments(last: 50) { nodes { author { login } } }
        timelineItems(since: $since, first: 100, itemTypes: [ISSUE_COMMENT, ASSIGNED_EVENT]) {
          nodes { __typename ...Comment ...Assigned }
        }
      }
      ... on PullRequest {
        id title url createdAt body isDraft author { ...Actor }
        comments(last: 50) { nodes { author { login } } }
        timelineItems(since: $since, first: 100,
                      itemTypes: [ISSUE_COMMENT, ASSIGNED_EVENT, REVIEW_REQUESTED_EVENT, MERGED_EVENT]) {
          nodes {
            __typename ...Comment ...Assigned
            ... on ReviewRequestedEvent {
              id createdAt actor { ...Actor }
              requestedReviewer { __typename ... on User { login } }
            }
            ... on MergedEvent { id createdAt url actor { ...Actor } }
          }
        }
        # Reviews are not read from the timeline: a review written over a long time
        # has an old createdAt, and the "since" filter can drop it.
        reviews(last: 30) {
          nodes {
            id submittedAt state url body author { ...Actor }
            comments(first: 50) { nodes { url body replyTo { author { login } } } }
          }
        }
      }
    }
  }
}
fragment Actor on Actor { __typename login }
fragment Comment on IssueComment { id createdAt url body author { ...Actor } }
fragment Assigned on AssignedEvent {
  id createdAt actor { ...Actor } assignee { ... on User { login } }
}
"""


class GhError(RuntimeError):
    pass


@dataclass
class Event:
    id: str
    at: datetime
    actor: str
    label: str
    url: str
    count: int = 1


@dataclass
class Alert:
    key: str
    group: str
    title: str
    subtitle: str
    message: str
    url: str


def gh(*args: str):
    proc = subprocess.run([GH, "api", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh api {args[0]} failed")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration(value: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mhd])", value)
    if not m:
        raise argparse.ArgumentTypeError("use a value like 30m, 6h or 2d")
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m[2]]: int(m[1])})


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def actions_url(repo: str, title: str) -> str:
    m = re.search(r"for (.+) branch$", title)
    query = f"?query={quote('branch:' + m[1])}" if m else ""
    return f"https://github.com/{repo}/actions{query}"


class Inbox:
    def __init__(self, cfg: dict, state: dict, since: datetime, dry_run: bool = False):
        self.cfg = cfg
        self.since = since
        self.dry_run = dry_run
        self.seen: dict[str, str] = state.setdefault("seen", {})
        self.me = gh("user")["login"]
        self.mention_re = re.compile(rf"(?<![\w-])@{re.escape(self.me)}(?![\w-])", re.I)

    def is_bot(self, actor: dict | None) -> bool:
        if not actor:  # deleted account
            return True
        if actor["login"] in self.cfg["ignore_users"]:
            return True
        return actor.get("__typename") == "Bot" and not self.cfg["include_bots"]

    def mentions(self, *texts: str | None) -> bool:
        return any(self.mention_re.search(t or "") for t in texts)

    def handle(self, t: dict) -> Alert | None:
        repo, reason, subject = t["repository"]["full_name"], t["reason"], t["subject"]
        if reason in SKIP_REASONS or any(fnmatch.fnmatch(repo, p) for p in self.cfg["ignore_repos"]):
            return None
        kind = subject["type"]
        if kind in ("PullRequest", "Issue") and subject.get("url"):
            return self.thread_alert(t)
        if not t["unread"]:
            return None
        if kind == "CheckSuite" and reason == "ci_activity":
            if self.cfg["ci_failures"] and " failed " in f"{subject['title']} ":
                return self.simple_alert(t, "CI failed", actions_url(repo, subject["title"]))
        elif kind == "WorkflowRun" and reason == "approval_requested":
            if self.cfg["deploy_approvals"]:
                return self.simple_alert(t, "Deployment waits for your approval", actions_url(repo, ""))
        elif reason in ("mention", "assign"):
            label = "Mentioned you" if reason == "mention" else "Assigned you"
            return self.simple_alert(t, label, f"https://github.com/{repo}")
        return None

    def simple_alert(self, t: dict, subtitle: str, url: str) -> Alert | None:
        key = f"{t['id']}:{t['updated_at']}"
        if key in self.seen:
            return None
        repo = t["repository"]["full_name"]
        return Alert(key, f"{repo}:{subtitle}", repo.split("/")[1], subtitle, t["subject"]["title"], url)

    def thread_alert(self, t: dict) -> Alert | None:
        m = SUBJECT_RE.search(t["subject"]["url"])
        read_at = parse_ts(t["last_read_at"]) if t.get("last_read_at") else None
        if not m or (read_at and read_at >= parse_ts(t["updated_at"])):
            return None
        owner, name, number = m.groups()
        data = gh("graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                  "-F", f"number={number}", "-f", f"since={iso(self.since)}")
        item = data["data"]["repository"]["issueOrPullRequest"]
        if not item:
            return None
        if item.get("isDraft"):
            if self.cfg["done_drafts"] and t["unread"]:
                self.mark_done(t, f"{owner}/{name}#{number} {item['title']}")
            if self.cfg["skip_drafts"]:
                return None

        mine = (item["author"] or {}).get("login") == self.me
        # The notification reason can stay "review_requested" after you comment, so also look for your
        # reviews and comments. The GitHub "participants" field leaves out the viewer, so it is not used.
        authors = item["comments"]["nodes"] + (item.get("reviews") or {}).get("nodes", [])
        took_part = t["reason"] in PARTICIPANT_REASONS or any(
            (n["author"] or {}).get("login") == self.me for n in authors)
        wants_comments = mine or (took_part and self.cfg["participant_comments"])
        team_request = t["reason"] == "review_requested"

        events = [self.description_event(item)]
        events += [self.timeline_event(n, item["url"], mine, wants_comments, team_request)
                   for n in item["timelineItems"]["nodes"]]
        events += [self.review_event(r, mine, wants_comments)
                   for r in (item.get("reviews") or {}).get("nodes", [])]
        events = [e for e in events if e and e.at > self.since and e.id not in self.seen
                  and not (read_at and e.at <= read_at)]
        if not events:
            return None

        by_actor: dict[str, list[Event]] = {}
        for e in events:
            by_actor.setdefault(e.actor, []).append(e)
        parts = []
        for actor, evs in by_actor.items():
            best = min(evs, key=lambda e: LABELS.index(e.label))
            n = sum(e.count for e in evs)
            text = f"{actor} left {n} comments" if best.label == "commented" and n > 1 else f"{actor} {best.label}"
            parts.append((LABELS.index(best.label), text))
        top = min(events, key=lambda e: LABELS.index(e.label))
        for e in events:
            self.seen[e.id] = iso(e.at)
        return Alert(
            key=top.id,
            group=f"{owner}/{name}#{number}",
            title=f"{name}#{number}",
            subtitle=", ".join(text for _, text in sorted(parts)),
            message=item["title"],
            url=top.url,
        )

    def mark_done(self, t: dict, ref: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if self.dry_run:
            print(f"{stamp} would mark done: {ref}")
            return
        gh(f"notifications/threads/{t['id']}", "-X", "DELETE")
        print(f"{stamp} marked done: {ref}")

    def description_event(self, item: dict) -> Event | None:
        # Only a new PR or issue passes the "since" filter, so an old description fires no alert.
        actor = item["author"]
        if self.is_bot(actor) or actor["login"] == self.me or not self.mentions(item["body"]):
            return None
        return Event(item["id"], parse_ts(item["createdAt"]), actor["login"], "mentioned you", item["url"])

    def timeline_event(self, node: dict, item_url: str, mine: bool, wants_comments: bool,
                       team_request: bool) -> Event | None:
        kind = node["__typename"]
        actor = node.get("author") or node.get("actor")
        if self.is_bot(actor) or actor["login"] == self.me:
            return None
        label = None
        if kind == "IssueComment":
            if self.mentions(node["body"]):
                label = "mentioned you"
            elif wants_comments:
                label = "commented"
        elif kind == "ReviewRequestedEvent":
            reviewer = node["requestedReviewer"] or {}
            if reviewer.get("login") == self.me:
                label = "requested your review"
            elif reviewer.get("__typename") == "Team" and team_request and self.cfg["team_review_requests"]:
                label = "requested your team's review"
        elif kind == "AssignedEvent":
            if (node["assignee"] or {}).get("login") == self.me:
                label = "assigned you"
        elif kind == "MergedEvent" and mine:
            label = "merged it"
        if not label:
            return None
        return Event(node["id"], parse_ts(node["createdAt"]), actor["login"], label, node.get("url") or item_url)

    def review_event(self, review: dict, mine: bool, wants_comments: bool) -> Event | None:
        actor = review["author"]
        if not review["submittedAt"] or self.is_bot(actor) or actor["login"] == self.me:
            return None
        comments = review["comments"]["nodes"]
        labels = []
        if mine:
            default = "reviewed" if review["body"] else "commented"
            labels.append({"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes"}
                          .get(review["state"], default))
        elif wants_comments and review["state"] == "COMMENTED":
            labels.append("commented")
        if self.mentions(review["body"], *(c["body"] for c in comments)):
            labels.append("mentioned you")
        if any(((c["replyTo"] or {}).get("author") or {}).get("login") == self.me for c in comments):
            labels.append("replied to you")
        if not labels:
            return None
        url = comments[0]["url"] if comments else review["url"]
        return Event(review["id"], parse_ts(review["submittedAt"]), actor["login"],
                     min(labels, key=LABELS.index), url, max(1, len(comments)))


def deliver(alerts: list[Alert], cfg: dict, dry_run: bool) -> bool:
    """Return False if an alert failed, so that the caller keeps the old state and retries."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for a in alerts:
        print(f"{stamp} {a.title} | {a.subtitle} | {a.message} | {a.url}")
    limit = max(1, cfg["max_alerts"])
    if len(alerts) > limit:
        extra = len(alerts) - limit + 1
        alerts = alerts[: limit - 1] + [Alert("", "gh-inbox:more", "GitHub", f"{extra} more updates",
                                              "Open your GitHub notifications.",
                                              "https://github.com/notifications")]
    if dry_run:
        return True
    ok = True
    for a in alerts:
        cmd = [NOTIFIER, "-title", a.title, "-subtitle", a.subtitle,
               "-message", a.message.lstrip("-"), "-open", a.url, "-group", a.group]
        if cfg["sound"]:
            cmd += ["-sound", cfg["sound"]]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if proc.returncode != 0:
            ok = False
            print(f"{stamp} terminal-notifier exit {proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print alerts, send nothing, save no state")
    parser.add_argument("--lookback", type=duration, help="check this period (30m, 6h, 2d), not the last run")
    parser.add_argument("--test", action="store_true", help="send one test alert")
    args = parser.parse_args()

    if not GH or not NOTIFIER:
        print("gh-inbox needs gh and terminal-notifier: brew install gh terminal-notifier", file=sys.stderr)
        return 1
    cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE)}
    if args.test:
        ok = deliver([Alert("", "gh-inbox:test", "gh-inbox", "Test alert", "Click to open GitHub notifications.",
                            "https://github.com/notifications")], cfg, dry_run=False)
        return 0 if ok else 1

    state = load_json(STATE_FILE)
    now = datetime.now(timezone.utc)
    if args.lookback:
        since = now - args.lookback
    elif "last_check" in state:
        since = parse_ts(state["last_check"]) - OVERLAP
    else:
        if not args.dry_run:
            save_state({"last_check": iso(now)})
        print("Baseline set. Alerts start from now. Use --lookback 1d --dry-run to see a sample.")
        return 0

    try:
        inbox = Inbox(cfg, state, since, args.dry_run)
        pages = gh(f"notifications?all=true&since={quote(iso(since))}&per_page=50", "--paginate", "--slurp")
    except (GhError, subprocess.TimeoutExpired) as err:
        print(f"{iso(now)} {err}", file=sys.stderr)
        return 1

    alerts = []
    for t in sorted((t for page in pages for t in page), key=lambda t: t["updated_at"]):
        try:
            alert = inbox.handle(t)
        except (GhError, subprocess.TimeoutExpired, KeyError, TypeError) as err:
            # Skip this thread so that one broken repo does not stop all alerts.
            print(f"{iso(now)} {t['repository']['full_name']}: {err}", file=sys.stderr)
            continue
        if alert:
            alerts.append(alert)
            if alert.key:
                inbox.seen[alert.key] = iso(now)
    if not deliver(alerts, cfg, args.dry_run):
        return 1  # keep the old state, so that the next run sends these alerts again

    if not args.dry_run:
        cutoff = now - SEEN_TTL
        state["seen"] = {k: v for k, v in inbox.seen.items() if parse_ts(v) > cutoff}
        state["last_check"] = iso(now)
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
