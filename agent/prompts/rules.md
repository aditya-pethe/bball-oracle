## Rules

- Emit exactly one SELECT statement. No INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/GRANT,
  no multiple statements, no semicolon-separated batches, no CTEs that write.
- Schema-qualify every table: `nba.shot_detail`, not `shot_detail`.
- Use ORDER BY whenever the question implies a ranking, a maximum, or a minimum.

- **Match the row count to what was asked.** A question with one answer gets
  `LIMIT 1` -- "the most common shot zone", "the longest made shot", "which team
  attempted the most threes" each name a single thing, and returning ten rows
  answers a question that was not asked. "Top 10", "the 10 players who...", "list
  the top five" get exactly that many. Only when a question clearly wants a
  ranking but names no size should you pick one (10 is a reasonable default) and
  say so in your summary.

- **Project the columns the question asks for, and no more.** If the question asks
  which players, return the player name -- not the name plus the id. Do not add
  identifier columns beside the human-readable ones, and do not surface every
  intermediate value you computed on the way to the answer. Extra columns are not
  free: they are what the user reads first, and burying the answer among
  supporting values makes the result harder to use, not richer.

  **One deliberate exception: a rate or percentage carries its denominator.** A
  shooting percentage without the attempt count behind it is not a smaller
  answer, it is a misleading one -- 100% on two shots and 48% on twelve hundred
  read identically otherwise. Return the count alongside the rate. The same goes
  for a share: give the numerator's basis.

- **If exact details are lacking you may use your own judgement to formulate a defensible answer**. For
  example, "who was the most efficient player last season?" You may use a metric like effective
  field goal % or similar, as long as you can explain your rationale. A user should not always have to 
  spell out exact columns to get a reasonable response, there may be non technical users. 
