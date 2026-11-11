# gh-inbox

A LaunchAgent that reads your GitHub notifications every 2 minutes and sends a macOS alert only for activity that needs you. Click an alert to open the comment, review or workflow run.

## What fires an alert

- A direct review request to you (team requests are off by default).
- A mention of you in a comment, a review or the description of a new PR or issue.
- A reply to your review comment, or an assignment to you.
- A review, comment or merge on your PR or issue, by a person.
- A new comment on a PR or issue that you commented on or reviewed.
- A failed workflow run that you started, or a deployment that waits for your approval.

Bots, your own actions and activity that you already read on GitHub never fire an alert. Draft PRs fire no alert, also when they are yours. More than 5 alerts in one run collapse into one "N more updates" alert.

## Install

```sh
brew install gh terminal-notifier   # if missing
gh auth login                       # the token stays in the gh keyring
git clone git@github.com:runejac/gh-inbox.git ~/gh-inbox && cd ~/gh-inbox
./install.sh                        # or ./install.sh 300 for every 5 minutes
python3 gh_inbox.py --test          # macOS asks to allow alerts on the first run
```

Notes:

- macOS 26 needs terminal-notifier 3.0 or later (`brew upgrade terminal-notifier`). Version 2.0.0 shows no alerts.
- If the test alert does not show, allow alerts for terminal-notifier in System Settings > Notifications.
- To update, run `git pull`. The LaunchAgent uses the new code at the next run.

The first run only records the time. Alerts start after that.

## Commands

| Command | Result |
| --- | --- |
| `python3 gh_inbox.py --dry-run --lookback 1d` | Prints the alerts for the last day. Sends nothing. |
| `python3 gh_inbox.py --test` | Sends one test alert. |
| `tail -f ~/.local/state/gh-inbox/gh-inbox.log` | Shows each alert that was sent. |
| `cat ~/.local/state/gh-inbox/gh-inbox.err.log` | Shows API errors. |
| `./install.sh --uninstall` | Removes the LaunchAgent. |

## Config

Optional. Put the keys that you want to change in `~/.config/gh-inbox/config.json`:

```json
{
  "ignore_repos": ["some-org/*"],
  "ignore_users": ["some-noisy-login"],
  "include_bots": false,
  "team_review_requests": false,
  "participant_comments": true,
  "skip_drafts": true,
  "done_drafts": false,
  "ci_failures": true,
  "deploy_approvals": true,
  "max_alerts": 5,
  "sound": "default"
}
```

`done_drafts` marks each new unread notification from a draft PR as done on GitHub, so it leaves your inbox. The log shows each thread that it marks. Set `"sound": ""` for silent alerts. The script reads the config at each run, so you do not need to reinstall.
