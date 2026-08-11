## Things that are easy to get wrong

**shot_detail contains FIELD GOALS ONLY -- no free throws.** This is the single most
important fact about this dataset. Any question about points, scoring, or totals
that is answered from shot_detail alone will silently undercount, because every free
throw is missing. Points = (field goals, worth 2 or 3, from shot_detail) PLUS (made
free throws, worth 1, from pbp_event where eventmsgtype = 3 and the description does
not start with 'MISS'). A scoring leaderboard built only from shot_detail runs
cleanly, looks authoritative, and is wrong.

**Only the 2023-24 REGULAR season is loaded** (season = 2023, season_type =
'regular'). There is no playoff data and no other season. A question about the
playoffs, about a different season, or about year-over-year change cannot be
answered -- say so rather than answering for 2023-24 and leaving the mismatch
unstated.

**There is no player biographical data** -- no height, weight, age, position, or
draft year. No awards, no salaries, no standings, no team win-loss records, no
box-score or season-aggregate tables. The data is event-grain only.

**Percentage questions need a minimum-attempts threshold** or the leaderboard fills
with players who went 1-for-1. If the question does not specify one, choose a
defensible threshold and state it in your summary.

**game_id has the leading '00' league code stripped** (22300001, not 0022300001).
