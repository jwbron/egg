# Feedback Comment Template

Use this template when creating feedback comments for open-ended questions.

## Template

```markdown
<!-- egg-feedback id=feedback-N -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: [Question 1]**

> _Your answer here_

**Q2: [Question 2]**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

---

*Authored-by: egg*
```

## Usage

### Agent: Creating Feedback

Use the CLI to create feedback comments:

```bash
egg-contract add-feedback \
  --question "What is the expected request volume?" \
  --question "Should we support legacy browsers?" \
  --format markdown
```

The `--format markdown` flag outputs the comment text ready for posting.

### Human: Responding

1. Edit the comment directly on GitHub
2. Replace `> _Your answer here_` with your answer
3. Optionally add any additional context in the "Additional Feedback" section
4. Check the "Submit feedback" checkbox when done

### Format Notes

- Keep answers in blockquote format (starting with `>`)
- Multi-line answers work — just start each line with `>`
- The "Additional Feedback" section is optional
- Only one feedback comment per phase is active at a time

## Integration

When feedback is submitted:

1. The orchestrator's decision queue detects the edit
2. Answers are parsed from the blockquotes
3. The contract is updated with the answers
4. The pipeline resumes with feedback available

The agent receives submitted feedback in its context via the orchestrator's prompt building.
