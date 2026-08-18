# SplitSub — MVP

A working prototype for splitting recurring shared subscriptions (Netflix, Spotify,
Costco, etc.) among friends/family/roommates, with monthly reminder messages.

## Run it locally
1. Install Python 3 if you don't have it.
2. In this folder, run:
   pip install -r requirements.txt
   python3 app.py
3. Open http://localhost:5055 in your browser.

The database is a local SQLite file (splitsub.db) created automatically on first run.

## What works right now
- Create a group, get a shareable join code
- Add members
- Add a subscription, split it by percentage (or one click to split equally)
- See each subscription's per-person cost
- Generate a ready-to-copy monthly reminder message per person (with total owed)

## What's intentionally NOT built yet (and why)
- No real user accounts/login — kept out to ship faster; add before a public launch
  so groups can't be edited by anyone who has the code.
- No automatic sending of reminders (email/SMS) — needs you to sign up for an email
  service (e.g. SendGrid, Postmark) or SMS service (e.g. Twilio) and hand me the API
  key; I can then wire up automatic sending. I can't create those accounts for you.
- No real payment collection — deliberately kept out of scope for v1. Reminders just
  point people to Venmo/PayPal/e-transfer, which avoids handling money or PCI compliance.
- No hosting yet — this runs on your machine for now. To make it live on the internet,
  deploy to a free tier on Render.com or Railway.app (both take about 10 minutes,
  no code changes needed) — you'll need to create that account yourself, then I can
  walk you through the deploy steps or do the config for you.

## Suggested next steps
1. Try it yourself locally with a real group (a few roommates or family members).
2. When ready to go live: create a free Render.com or Railway.app account, tell me,
   and I'll write the exact deploy config.
3. When ready for real reminders: create a free-tier SendGrid or Twilio account,
   give me the API key, and I'll wire up automatic monthly sending.

## Deploying to Render.com (free tier)

1. Create a free account at render.com (this step is yours to do — I can't create
   accounts on your behalf).
2. Push this folder to a new GitHub repo (or use Render's "deploy from a zip/folder"
   option if offered).
3. In Render, click "New +" → "Blueprint", point it at the repo — it will read the
   `render.yaml` file already included here and auto-configure everything
   (build command, start command, Python version). No manual setup needed.
4. Click deploy. Render gives you a live URL like splitsub.onrender.com within a
   couple of minutes.

Note: the free tier's disk is not persistent across redeploys, so the SQLite database
will reset if you redeploy or the service sleeps for inactivity and restarts. That's
fine for testing with real users early on; before a real public launch, swap in
Render's free PostgreSQL add-on so data survives — tell me when you're ready and I'll
make that change (it's a small edit to the database connection code).
