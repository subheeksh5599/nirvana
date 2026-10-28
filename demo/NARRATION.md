# Nirvana demo: narration

Voice: Microsoft `en-US-AndrewMultilingualNeural` through edge-tts, natural pace. Switching voice is the `VOICE` line in `words.py`.

Final file: intro 2:06.7 (HyperFrames, `intro/`), then the screen capture, unedited, with narration. Last frame held 1.5 s.

## Intro (rendered)

| Final time | Scene | Line |
|---|---|---|
| 0:00.7 | bill | Azure Databricks is moving Standard workspaces to Premium. For many teams, that's thirty-five percent more, or higher. |
| 0:09.4 | bill | So they want to move their queries somewhere cheaper, like Zetaris. |
| 0:14.3 | zip | But here's the scary part. A migrated query rarely crashes. It runs fine, and quietly gives a different answer. |
| 0:22.4 | zip | Same table, same row. Databricks says zip code zero, nine, three, two, one. The copy on Zetaris says nine, three, two, one. The leading zero is gone. |
| 0:36.4 | title | Nirvana catches this before you move. Query by query, it proves which ones are safe to leave Databricks. |
| 0:44.9 | how | First, it measures every table on both engines. If the data itself differs, those queries are blocked, not guessed. |
| 0:53.3 | how | Then it runs each query on Databricks and on Zetaris, and compares every row. Each answer gets a fingerprint. |
| 1:01.2 | how | If the answers differ, it tries a fix on Zetaris. A fix is kept only when it returns Databricks' exact rows. |
| 1:10.0 | agent | When something still doesn't match, an AI agent takes over. NVIDIA Nemotron, running through Meterless Nightshift, picks the next check. |
| 1:19.9 | agent | Zetaris runs it. Here, 1,351 zip codes differ. The agent proposes padding them back to five digits, and Zetaris confirms it. Zero rows differ. |
| 1:32.3 | agent | The AI chooses what to look at. It never decides what's true. The rows do. |
| 1:38.9 | memory | Every proof is saved in Meterless H-MEM, so the agent remembers. The next time, the same table took two steps instead of four. |
| 1:49.3 | payoff | Left to run overnight on a CSV migration, forty-seven of a hundred and three queries were provably safe at first. By morning, a hundred. |
| 1:59.0 | payoff | The last three stay on Databricks, each with its reason. |
| 2:04.3 | handoff | Now let's watch it run, live. |

## Screen capture

| Final time | Recording time | Line |
|---|---|---|
| 2:07.3 | 0:00.6 | This is Nirvana's landing page. It leads with two real runs. |
| 2:12.4 | 0:05.7 | Loaded from CSV exports, forty-seven of a hundred and three queries could move. From identical parquet files, a hundred and two. |
| 2:21.3 | 0:14.6 | Every verdict comes from rows both engines really returned. Align the inputs, run on both, compare every row, and prove the cause. |
| 2:33.5 | 0:26.8 | Step one, start a check. The latest result is the CSV migration, where fifty-six queries still need a closer look. |
| 2:43.1 | 0:36.4 | I'll check query seven. |
| 2:45.9 | 0:39.2 | Nirvana first measures every table the query reads, on both engines. Row counts, nulls, minimums, maximums, and exact sums. |
| 2:56.7 | 0:50.0 | If any table differed, the query would be blocked, not judged. Comparing answers over different data proves nothing. |
| 3:05.5 | 0:58.8 | Each table matches. Customer demographics alone has almost two million rows on each side. |
| 3:13.5 | 1:06.8 | Then the query runs on Databricks and on Zetaris, and every row of both answers is compared. |
| 3:24.5 | 1:17.8 | Query seven moves. Same answer on both engines. |
| 3:31.3 | 1:24.6 | Here's the proof. A hundred rows on each side, the same fingerprint on both, and no column that differs. |
| 3:39.3 | 1:32.6 | Below are the exact query texts each engine ran, so anyone can repeat the check. |
| 3:47.5 | 1:40.8 | Now the harder case. Back to the CSV migration, with fifty-six queries waiting. Let's hand them to the agent. |
| 3:55.6 | 1:48.9 | Step three, fix with the agent. It lists what blocked the migration. Three tables differ between the engines, and query sixty-six gives an answer no rule could fix. |
| 4:10.1 | 2:03.4 | All three tables are already proven. The agent explained every difference, and this button re-checks the fifty-five queries they blocked. |
| 4:21.1 | 2:14.4 | I asked the agent to try query sixty-six again, live. NVIDIA Nemotron, running through Meterless Nightshift, decides what to check next. |
| 4:32.7 | 2:26.0 | Before its first step, it asked Meterless H-MEM what it already knew. H-MEM recalled one earlier finding about this query. |
| 4:42.7 | 2:36.0 | Earlier investigations stay on the page. The customer address table took two steps, seventy-nine seconds. Each step is one call to the free Nemotron tier. |
| 4:56.3 | 2:49.6 | Here's its first step. Nemotron's idea is to cast all thirty-six numeric outputs to the exact types Databricks declares. |
| 5:05.3 | 2:58.6 | Zetaris ran it, and the rows still differ, so the idea is rejected. The model proposed it, and the rows refused it. |
| 5:14.3 | 3:07.6 | That's the rule everywhere in Nirvana. Nothing counts until the rows match Databricks. |
| 5:21.3 | 3:14.6 | So query sixty-six stays on Databricks for now, and the rows that differ are its reason. |
| 5:32.3 | 3:25.6 | The overnight audit runs all of this as one job. Last night, forty-seven became a hundred. |
| 5:39.3 | 3:32.6 | I've started a new one. It runs every query, investigates what's blocked, re-checks, installs guard rules, and writes a morning report. Start it before bed, read it over coffee. |
| 5:53.5 | 3:46.8 | It takes about half an hour. |
| 5:57.3 | 3:50.6 | A finished investigation. H-MEM recalled two findings, so the agent went straight to the proof. Every padded zip code matches. |
| 6:11.7 | 4:05.0 | The tables page shows this run's alignment. Every table has the same row count on Databricks and on Zetaris. |
| 6:21.1 | 4:14.4 | The row diff is Zetaris reading Databricks over JDBC and its own sample on S3, in a single query. |
| 6:29.3 | 4:22.6 | A hundred and ninety rows exist only in Databricks. Three hundred thousand, one hundred and ninety exist only in the Zetaris sample, mostly one catalog page, repeated. |
| 6:45.1 | 4:38.4 | Next, guard rules for after the move. |
| 6:49.9 | 4:43.2 | That gap is why Nirvana measures a copy before trusting it. |
| 6:55.9 | 4:49.2 | Nirvana measures each column on Databricks and installs that shape as Zetaris data quality rules. I'm installing them for the customer address table. |
| 7:06.9 | 5:00.2 | The web site table's rules from earlier all hold. Now Zetaris runs every rule across all fifty thousand customer addresses. |
| 7:21.1 | 5:14.4 | The zip rule breaks on exactly the rows that lost their leading zero. Zetaris keeps enforcing it after every load. |
| 7:30.3 | 5:23.6 | What H-MEM remembers. |
| 7:33.0 | 5:26.3 | The query workload. |
| 7:35.5 | 5:28.8 | And every past run. |
| 7:39.3 | 5:32.6 | Back on the audit, the new run is still on its first step, running every query on both engines. |
| 7:51.3 | 5:44.6 | Everything here runs on this Mac. Docker holds the Zetaris platform, the data layer under every check. |
| 8:03.3 | 5:56.6 | And this is Meterless Nightshift, the AI provider. Every Nemotron call the agent made went through it. |
| 8:11.3 | 6:04.6 | That's Nirvana. The model picks what to check, and the rows decide what can leave Databricks. |

Every line is gated by `gate.py`: it must end before its screen changes and start at least 0.8 s after the previous one.
Nothing was cut from the recording. Numbers in the lines are read off the screen or the recorded runs.
