# Semantic A/B (20260911T043648Z)

Model: `deepseek-chat` · corpus: intent.jsonl (50)

| Metric | A: JSON mode | B: strict tool |
|---|---|---|
| schema_valid | 50 | 50 |
| internal_accuracy | 40 | 37 |
| behavioral_correctness | 41 | 40 |
| failures | 0 | 0 |
| mean_ms | 1054 | 1982 |
| median_ms | 1031 | 1370 |
| p95_ms | 1484 | 3491 |

## B failures (behavioral)
- 'Create a topic for CS188.': expected settings_update -> create_item (WRONG_INTENT)
- 'This topic is for my basketball club.': expected settings_update -> update_item (WRONG_INTENT)
- 'Make a topic for my UAV research.': expected settings_update -> create_item (WRONG_INTENT)
- 'This is where I keep track of groceries.': expected settings_update -> unknown (WRONG_INTENT)
- 'Add this document to CS188.': expected create_item -> link_items (WRONG_INTENT)
- 'Save this with my UAV research.': expected create_item -> link_items (WRONG_INTENT)
- 'Use my pantry for this grocery topic.': expected link_items -> update_item (WRONG_INTENT)
- "Remember that I don't like scheduling after 10 PM.": expected memory_learn -> settings_update (WRONG_INTENT)
- 'Track my project.': expected tracker_create -> project_status (WRONG_INTENT)
- 'Do that tomorrow.': expected unknown -> defer (WRONG_INTENT)
