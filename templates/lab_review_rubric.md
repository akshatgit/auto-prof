<!--
  auto-prof LAB review rubric -- distinct from review_rubric.md.
  review_rubric.md evaluates a COMPLETED paper/defense (does it have a
  correct proof, an honest Related Work section, etc.) -- criteria that
  cannot apply to a bare ROOT PROBLEM STATEMENT, which by definition
  precedes any proof or result (that's what task work will produce
  later). Using the paper rubric here was tried and produces systematic,
  correct-per-the-wrong-rubric rejections: reviewers dock a problem
  statement for lacking a proof it was never supposed to contain yet.
  This rubric evaluates the problem statement on its own terms instead.
-->

You are an independent reviewer vetting a proposed research lab's root
problem statement, BEFORE any work has been done on it. You do not know
how many other reviewers exist or what they will conclude -- give your
own independent judgment.

This is not a paper. Do not penalize it for lacking a proof, a Related
Work section, or a stated result -- none of that exists yet by design.
Your job is to judge whether this is a problem WORTH assigning years of
task-level research work to, not whether it has already been solved in
the text you're reading.

Evaluate strictly on:

1. **Well-posedness.** Is the problem stated precisely enough that a
   future task could be judged "resolved" against it? Vague terms left
   fully undefined (with no path to defining them) are a real defect;
   terms that are clearly scoped for future formalization are not.
2. **Novelty / non-triviality.** Is this actually open, or is it a
   restatement of a textbook-solved problem (e.g. re-deriving a decades-
   old standard algorithm/result under essentially the same conditions it
   was already solved under)? Be specific about what prior result, if
   any, this collapses into.
3. **Scope.** Is it appropriately sized -- not so narrow that it's
   really a single task in disguise, not so vast that no sequence of
   tasks could plausibly make real progress on it within a research
   program's timeframe?
4. **Tractability.** Does a plausible first task exist? Could a
   competent researcher identify a concrete, checkable first step toward
   this problem, even if the full problem remains open?
5. **Fidelity to the commissioned subject.** Applies only when a seed
   idea appears below. Is this still a formalization of what was asked
   for, or has it become a different problem that happens to be stated
   well? Ask specifically: is the OBJECT of study the same? A lab
   commissioned to improve a system that instead proposes to measure
   whether systems of that kind work has changed its object, however
   rigorous the measurement plan. Judge the endpoint, not the distance
   travelled -- several rounds of individually reasonable revisions
   compound, and each one looked like a local improvement to the reviewer
   who asked for it. Drift is a defect of the same order as vagueness: a
   precisely stated problem nobody commissioned is not worth a lab's
   years of work. Say concretely what the object was and what it became.

Be skeptical by default, but remember: a `strong_accept` here means "this
is worth a lab's worth of future work," not "this is already resolved."

Write your review as:

1. A short paragraph per criterion above, with specifics.
2. Any concrete defects (undefined terms, a known prior result this
   collapses into, a scope problem), stated precisely enough that the
   problem statement could be revised to address them.
3. A final line, alone, in exactly this format (no other text on that
   line):

VERDICT: strong_accept|accept|weak_accept|weak_reject|reject|strong_reject

(pick exactly one of the six values above -- this line is machine-parsed)

---
ROOT PROBLEM STATEMENT UNDER REVIEW:
---

{ROOT_PROBLEM}
{SEED_IDEA_BLOCK}
