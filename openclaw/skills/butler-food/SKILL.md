---
name: butler-food
description: >-
  Manage food inventory, meals, recipes, and grocery lists. Use for "what's
  expiring", "what can I cook", "add milk", "I ate the eggs", recipe suggestions,
  groceries to buy, or favorites.
metadata:
  openclaw:
    requires:
      bins:
        - python
        - curl
---

# Butler Food

Food state lives in Butler's inventory and recipe library. Read it fresh every
time; quantities and expiry are real and change.

## Workflow

1. **Inventory**: `pantry` (all) or `expiring` (within N days) for "what will go
   bad". `context` also surfaces `food_expiring`.
2. **What to cook**: `recipe` (budget_minutes) for a meal suggestion that fits
   available time + ingredients, or `recipe_search` (query) for real online
   recipes, then `rate_recipe` / `favorite` to store what you liked.
3. **Groceries**: `grocery` for a deterministic deduped list.
4. **Add/consume**: `task`-style writes are additive — the model submits the
   change; Butler computes. For inventory edits use the `/food` HTTP routes or
   `pantry` read + a guarded apply (see README). Prefer describing the change and
   letting the operator confirm.

## Rules

- Always read `pantry`/`expiring` before claiming what's in stock. Never guess
  quantities or expiry dates.
- `quantity` is a float; `unit` a string (oz, g, ml, each). Pass sensible units
  or the parser may mis-handle them.
- Meal suggestions must respect the returned `shortfall`s (missing ingredients) —
  tell the user what's missing.
- Treat add/consume of food as guarded writes requiring operator approval.
