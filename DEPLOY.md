# Deploying the Backend (free, on Render)

Right now your Flutter app only works while your PC is on and running uvicorn.
Deploying the API means the app works **anywhere** — and it's a prerequisite for
ever putting this on a real phone or the Play Store.

---

## What you're deploying

A **separate repo** containing just the API and the Python engine it needs.
Not your Streamlit app — that stays where it is.

---

## Step 1 — Create a new GitHub repo

Call it something like `research-api`. **Public** is fine (there are no secrets
in the code — the API key goes in Render's dashboard, never in the repo).

## Step 2 — Add these files to it

From your `C:\MyDashboard` folder, upload:

```
api.py                  <- the FastAPI app
analysis_api.py         <- your analysis engine
pf_doctor.py            <- Portfolio Doctor
pf_xray.py              <- Portfolio X-ray
cache_compat.py         <- NEW: lets the modules run outside Streamlit
requirements-api.txt    <- rename to requirements.txt
render.yaml             <- deployment config
```

**Important:** rename `requirements-api.txt` to **`requirements.txt`** in the new
repo. Render looks for that exact name.

**Do NOT include:** `dashboard.py`, the `pages/` folder, or anything Streamlit.
The API doesn't need them, and they'd slow the build.

## Step 3 — Deploy on Render

1. Go to **render.com** → sign up (free, GitHub login works)
2. Click **New +** → **Web Service**
3. Connect your GitHub, pick the `research-api` repo
4. Render reads `render.yaml` and fills most fields in. Check:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn api:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Free
5. Under **Environment Variables**, add:
   - Key: `GEMINI_API_KEY`
   - Value: your Gemini key
6. Click **Create Web Service**

First build takes ~5 minutes.

## Step 4 — Get your URL

Render gives you something like:

```
https://research-platform-api.onrender.com
```

**Test it in a browser:**

```
https://research-platform-api.onrender.com/docs
```

You should see the same interactive API page you saw on localhost.
Try `/stock/RELIANCE` — if real data comes back, you're deployed.

## Step 5 — Point the Flutter app at it

In `lib/services/api_service.dart`, change one line:

```dart
// from:
static const String baseUrl = 'http://localhost:8000';

// to:
static const String baseUrl = 'https://research-platform-api.onrender.com';
```

Hot-restart the app (`R`). The connection banner should still say
**Connected to backend** — but now it's talking to the cloud, not your PC.

You can now close the uvicorn window. The app no longer needs your machine.

---

## The free tier catch — read this

Render's free plan **sleeps the service after 15 minutes of inactivity**.
The next request has to wake it, which takes **30–60 seconds**.

So the first stock lookup after a quiet period will feel very slow, then be
fast again. That's not a bug in your code.

**Options:**
- Live with it (fine for personal use / testing)
- Ping the API every 10 minutes with a free uptime monitor (e.g. UptimeRobot)
  to keep it awake
- Upgrade to Render's paid tier (~$7/month) for always-on

I'd live with it for now. Don't pay until you have users.

---

## What changed in your code (and why)

`pf_doctor.py` and `pf_xray.py` used `@st.cache_data`, which **only works inside
a running Streamlit app**. On a plain API server it would fail.

Rather than maintain two copies of the Portfolio Doctor's maths (which would
inevitably drift apart and one would quietly go wrong), I added
`cache_compat.py`. It detects whether Streamlit is running:

- **In your Streamlit app** → uses `st.cache_data`, exactly as before
- **On the API server** → falls back to a plain in-memory TTL cache

One source of truth, two runtimes. Your Streamlit app is unaffected — I tested
that the diagnostics return identical results either way.
