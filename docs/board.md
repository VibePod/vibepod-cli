# Board automation

`vp board work` lets an agent work through a project on
[vibepod-board](https://github.com/VibePod/vibepod-board). It takes the next
**Planned** task, implements it on a branch of its own, and moves the task to
**Review** when done:

1. **Claim** the next planned task in the board's work order. Tasks whose
   dependencies are not done, blocked tasks and tasks someone holds are
   skipped. The card moves to In Progress, with the worker as its holder.
2. **Check out** the task's branch in a git worktree of its own, next to the
   repository. The repository's own checkout is left alone.
3. **Run the agent** headless, like `vp task create`, with the task's title,
   description and acceptance criteria as the prompt. The agent commits its
   work; anything it leaves uncommitted is committed for it. It ends its run
   with a short structured result: done, needs input (with its question), or
   failed (with the reason).
4. **Verify** with an optional command, such as the test suite, run in the
   worktree.
5. **Hand over** the task to Review with its branch name, or give it back to
   Planned (or block it) with a note saying why.

Every run adds a report to its task on the board: the agent's summary, the
commits it made, the verify output, the duration and the reason when it
failed.

Started with `--mode review`, the worker reviews the tasks in Review instead,
and decides whether they need rework or are ready for a pull request (see
[Review workers](#review-workers)).

## Connect to the board

Create an API token for the project on the board (**API Tokens**), then point
VibePod at the board in your config:

```yaml
# ~/.config/vibepod/config.yaml
board:
  url: http://localhost:3000
  token: vbp_... # project-scoped board API token
```

or in the environment, which wins over the config:

```bash
export VP_BOARD_URL=http://localhost:3000
export VP_BOARD_TOKEN=vbp_...
```

The token stays with `vp`: it is never passed to the agent's container.

## Example

Work through the planned tasks of project `VP` with Claude Code, in a local
checkout, running the tests before each hand-over:

```bash
vp config allow-dir ~/src/vibepod-cli       # once per repository
vp board work VP --agent claude \
  --repo ~/src/vibepod-cli \
  --verify "python -m pytest -q" \
  --ikwid
```

```text
Connected to the board as claude@laptop
Claimed VP-12: Add `vp board work`
Working on branch issue-201 in /home/me/src/vibepod-cli-worktrees/issue-201
Starting task on claude with image vibepod/claude:latest
Agent running as task 3f2c9a1b7e40 (vp task logs 3f2c9a1b7e40)
Verifying with: python -m pytest -q
Handed VP-12 over to Review on branch issue-201
No planned task can be claimed
Signed off: No planned task left to claim
Handed over 1 task(s), gave back 0.
```

Follow the agent while it works with `vp task logs <id> --follow`; every run is
a regular [background task](cli-reference.md).

To keep going as new tasks are planned, poll instead of exiting:

```bash
vp board work VP --agent codex --repo ~/src/app --poll 2m
```

## Options

| Option | Meaning |
| --- | --- |
| `--agent`, `-a` | The agent that implements or reviews the tasks (required; it needs headless mode). |
| `--mode implement\|review` | Implement planned tasks (default), or review the tasks in Review (see [Review workers](#review-workers)). |
| `--name` | The worker's name on the board, and the holder of its claims. Defaults to `<agent>@<host>`, or `<agent>-review@<host>` for a review worker. Give two workers on one machine different names. |
| `--label` | Only claim tasks carrying this label; repeat to require more. |
| `--min-readiness` | Only claim tasks with at least this readiness score. |
| `--task` | Work on this one task, such as `VP-12`, then exit. |
| `--once` / `--max N` | Exit after one / after `N` tasks. |
| `--poll 2m` | When no task is left, wait and look again instead of exiting. |
| `--repo` | The repository to work in. Defaults to the task's local repository path on the board. |
| `--worktree-dir` | Where task worktrees go. Defaults to `<repo>-worktrees` next to the repository. |
| `--base` | Where new branches start, and what a review diffs the branch against. Defaults to the repository's current branch. |
| `--branch-template` | The branch name, from `{issue}` (GitHub issue number), `{number}` (task number), `{key}` (`vp-12`) and `{project}` (`vp`). Defaults to `issue-{issue}`; a task without a GitHub issue gets `{key}`, such as `vp-12`. |
| `--existing continue\|refuse` | When the task's branch or worktree exists, such as from an earlier attempt: continue on it (default) or refuse, which blocks the task. |
| `--keep-worktree` | Keep the worktree after the hand-over or review; by default it is removed, and the branch stays. |
| `--profile`, `--provider`, `--ikwid`, `-e/--env`, `--network`, `--no-overlay` | Passed to every agent run, as for `vp task create`. |
| `--timeout 2h` | Time limit per task (`none` for no limit). |
| `--verify` | A command that must pass in the worktree before the task moves to Review. In review mode, a failing command sends the task back for rework. |
| `--on-fail planned\|blocked` | Where failed and timed-out tasks go: back to Planned, counting a failed attempt (default), or blocked. The board blocks a task after too many failed attempts. |
| `--max-attempts` | Failed attempts before the board blocks a task. |
| `--parallel` | Run alongside other workers on the same profile (see below). |
| `--usage-limit-wait 30m` | How long to pause after a usage limit when the agent names no reset time. |

## Questions and rework

The prompt asks the agent to end every run with a result block:

```text
<vibepod-result>
{"status": "done", "summary": "Added the command and its tests."}
</vibepod-result>
```

`status` is `done`, `needs_input` with a `question`, or `failed` with a
`reason`. A run whose output has no readable result counts as failed.

- **Needs input.** When the task is unclear, the agent asks instead of
  guessing. The task is blocked on the board with the question on its card,
  without counting a failed attempt, and the work so far stays on the branch.
  Once someone answers on the board, the task is planned again, and the next
  run gets the question and the answer in its prompt.
- **Rework.** When a reviewer sends a task back from Review with feedback, the
  next run continues on the branch the task was handed over on and gets the
  feedback in its prompt, instead of starting from scratch.

## Review workers

A review worker answers one question about a task in Review: does it need
rework, or can its pull request be opened? It runs in the same repository and
worktree folder as the implementation workers, and several, with different
agents, can review the same task:

```bash
# implements
vp board work VP --agent codex --repo ~/src/app --poll 2m
# reviews
vp board work VP --agent claude --repo ~/src/app --poll 2m --mode review \
  --verify "python -m pytest -q"
# a second reviewer
vp board work VP --agent codex --repo ~/src/app --poll 2m --mode review \
  --name codex-review-2
```

The board moves each task along:

```text
Planned ──▶ In progress ──▶ Review ──▶ PR ready
   ▲                          │
   └──────── rework ──────────┘
```

How many approvals a task needs is the project's **required approvals**
setting on the board (1 by default; edit it in the project dialog). Each
reviewer name counts once: `claude-review@laptop` and `codex-review-2` make two
approvals. Once a task's head commit has the approvals it needs, it moves to
**PR ready**. A rework verdict sends it back to **Planned** with the feedback,
where an implementation worker continues on its branch, and ends the other
reviews of the task. After the project's maximum of rework rounds in a row,
the task is blocked in Review for a person instead.

For each task, a review worker:

1. **Claims** the next task in Review for a review. The card stays in Review;
   the claim names the commit the hand-over put up for review.
2. **Checks out** exactly that commit, detached, in a worktree of its own
   (`<worktree-dir>/review-<key>-<worker>`), so reviewers of one task never
   share one. No branch is created or moved. A missing branch or commit
   blocks the task in Review with the reason.
3. **Runs the agent** with a review prompt: the task's title, description and
   acceptance criteria, its history (questions, answers, earlier feedback and
   reviews), and the base to inspect `git diff <base>...HEAD` and the commit
   log against. The agent only reads: it must not change files or commit.
4. **Verifies** with the optional `--verify` command. A failing command always
   sends the task back for rework, with its output as the feedback.
5. **Sends the verdict** from the agent's result block:

    ```text
    <vibepod-result>
    {"status": "rework", "summary": "Close, but untested.",
     "feedback": ["Add a test for an empty input.", "Handle a missing file."]}
    </vibepod-result>
    ```

    `status` is `approve`, `rework` with `feedback` (concrete points the next
    implementation run gets), `needs_input` with a `question` (the task is
    blocked in Review until someone answers), or `failed` with a `reason`. A
    run without a readable result fails the review, and the task stays in
    Review for other reviewers. Every review adds a run report with its
    verdict, summary, feedback and verify output.
6. **Removes** its worktree, unless `--keep-worktree`. The branch is never
   touched.

A review must leave the repository as it found it. The agent gets the git
directory read-only, and if it committed, switched branches, moved refs or
left changes in its worktree anyway, all of it is thrown away and the review
fails with that reason. The same check runs again after `--verify`, which runs
the code under review; files it leaves behind, such as caches, are fine.

When the task moved on during the review, because it was handed over again or
another reviewer sent it back, the review ends without a verdict. Pause, stop,
cancel, `--parallel`, the profile lock and usage limits work as for
implementation workers, and a review the board cannot renew is given up before
its lease runs out. `--branch-template`, `--existing`, `--on-fail` and
`--max-attempts` have no meaning for a review and are refused with
`--mode review`.

## Safety

The agent runs in its container; the worker keeps it there:

- Git on your machine never runs hooks or an fsmonitor while the worker uses it. The agent
  container can commit to the repository, but its git configuration, hooks, the
  worktree's pointers into the repository, and the HEAD and index of your own checkout
  are read-only there. A run that changed them anyway, moved other branches or tags
  (they are put back), or left its own branch blocks the task for a look instead of
  being handed over. Branches others move meanwhile are left alone: those of other
  tasks, and a branch you commit to in your own checkout.
- Only the worktree the worker made for a task, in the worktree folder, is reused or
  removed. A branch checked out anywhere else, such as in your own checkout, is never
  taken over, even when the worktree folder holds it.
- A repository named by a task on the board must be on the allowed directories list,
  like any `vp task` workspace, before the worker touches it.
- `--verify` runs on your machine, in the worktree, with the agent's changes: it runs
  code the agent wrote with your permissions. The board token is left out of its
  environment. Use it in repositories you would run the agent's tests in yourself, or
  make the command run them in a container.

## Subscriptions and usage limits

The agent runs with the saved login of its [profile](profiles.md), so Claude
Code and Codex subscription logins work as they do for `vp run`. Log in once
with `vp run claude` (or `vp run codex`) and keep the profile for the worker.

Only one task runs at a time per profile, across all `vp board work` processes
on the machine, since runs on one login share its limits. A second worker on
the same profile waits for the first to finish its task; `--parallel` turns
the lock off.

When the agent stops at a usage limit, the task goes back to Planned without
counting a failed attempt (also when the agent exits cleanly with a limit
message and no work), and the worker pauses until the limit resets (when
the agent says when) or for `--usage-limit-wait`. The board shows the worker as
paused with the reason.

## The board's controls

The worker registers with the board when it starts and signs off when it stops,
so the board lists it with its status, current task and step. A heartbeat
every few seconds keeps the worker online and its claim alive, and the reply
carries the board's instructions:

- **Pause** automation of the project: the worker finishes its current task
  and takes no new one until automation is resumed.
- **Stop** the worker: the run in progress ends, its task goes back to
  Planned, and the worker signs off.
- **Cancel** a run: the task is already back in Planned; the worker ends the
  run and reports it as cancelled.

`Ctrl+C` stops the worker the same way. If the worker itself fails with an
unexpected error, it gives the task back to Planned before it exits, so the task
does not stay claimed until its lease runs out.
