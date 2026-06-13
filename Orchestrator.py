"""
AI Orchestrator — Browser Automation Pipeline
=============================================
Sends your question to ChatGPT, Gemini, and Claude simultaneously,
then feeds all three responses to a 4th Claude instance that
synthesizes them into one final, high-quality answer.

SETUP (run once):
  pip install playwright
  playwright install chromium

USAGE:
  python ai_orchestrator.py
  python ai_orchestrator.py --question "Explain the Fermi paradox"
  python ai_orchestrator.py --headed   # see the browsers live
"""

import asyncio
import argparse
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

# ─────────────────────────────────────────────
#  CONFIG — edit these if selectors break
# ─────────────────────────────────────────────

CHATGPT_URL   = "https://chatgpt.com/"
GEMINI_URL    = "https://gemini.google.com/app"
CLAUDE_URL    = "https://claude.ai/new"

# How long (seconds) to wait for a response before giving up
RESPONSE_TIMEOUT = 120

# How long to wait after the AI appears to stop (to catch late edits)
SETTLE_WAIT = 3

CHATGPT_ROLE = "You are the Researcher. Answer the question as thoroughly and accurately as possible, citing key facts. Be detailed."
GEMINI_ROLE  = "You are the Devil's Advocate. Answer the question, but actively look for weaknesses, edge cases, or things that are commonly misunderstood about it. Provide a nuanced answer."
CLAUDE_ROLE  = "You are the Specialist. Answer the question with depth, focusing on the most technically or conceptually precise explanation possible."

SYNTHESIZER_PROMPT = """You are a Master Synthesizer. Three different AI assistants have each answered the same question. Your job is to produce ONE final, definitive answer that:

1. Combines the strongest points from all three
2. Removes any inaccuracies or weak reasoning
3. Incorporates the nuance and caveats where they improve the answer
4. Is better than any single response alone
5. Is clearly written and well-structured

---
ORIGINAL QUESTION:
{question}

---
RESPONSE FROM AI #1 (Researcher — ChatGPT):
{response_chatgpt}

---
RESPONSE FROM AI #2 (Devil's Advocate — Gemini):
{response_gemini}

---
RESPONSE FROM AI #3 (Specialist — Claude):
{response_claude}

---
Now write the single best possible answer to the original question, synthesizing all three perspectives:"""


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def print_banner():
    print("\n" + "═" * 60)
    print("  🤖  AI Orchestrator — Multi-Model Pipeline")
    print("═" * 60)

def print_step(step: str, detail: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] ▶  {step}")
    if detail:
        print(f"         {detail}")

def print_response(name: str, text: str):
    print(f"\n{'─'*60}")
    print(f"  {name}")
    print('─'*60)
    wrapped = textwrap.fill(text[:800], width=58, subsequent_indent="  ")
    print(f"  {wrapped}")
    if len(text) > 800:
        print(f"  ... [{len(text)-800} more characters]")


# ─────────────────────────────────────────────
#  BROWSER HELPERS
# ─────────────────────────────────────────────

async def wait_for_typing_to_stop(page: Page, stable_selector: str, timeout: int = RESPONSE_TIMEOUT):
    """
    Polls the page until the response text hasn't changed for SETTLE_WAIT seconds,
    indicating the AI has finished generating.
    """
    last_text = ""
    stable_since = None
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        try:
            elements = await page.query_selector_all(stable_selector)
            current_text = " ".join([await el.inner_text() for el in elements if el])
        except Exception:
            current_text = ""

        if current_text and current_text == last_text:
            if stable_since is None:
                stable_since = asyncio.get_event_loop().time()
            elif asyncio.get_event_loop().time() - stable_since >= SETTLE_WAIT:
                return current_text
        else:
            stable_since = None
            last_text = current_text

        await asyncio.sleep(0.8)

    return last_text  # Return whatever we have on timeout


async def type_and_submit(page: Page, input_selector: str, submit_selector: str, text: str):
    """Click the input, type text, and press Enter or click submit."""
    await page.wait_for_selector(input_selector, timeout=30000)
    await page.click(input_selector)
    await page.keyboard.type(text, delay=20)
    await asyncio.sleep(0.5)

    try:
        btn = await page.query_selector(submit_selector)
        if btn:
            await btn.click()
        else:
            await page.keyboard.press("Enter")
    except Exception:
        await page.keyboard.press("Enter")


# ─────────────────────────────────────────────
#  INDIVIDUAL AI SCRAPERS
# ─────────────────────────────────────────────

async def ask_chatgpt(context: BrowserContext, question: str) -> str:
    page = await context.new_page()
    try:
        print_step("ChatGPT", "Opening page...")
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        full_prompt = f"{CHATGPT_ROLE}\n\nQuestion: {question}"

        # ChatGPT uses a contenteditable div or textarea
        input_sel = "#prompt-textarea, [data-id='composer-text-input'], textarea[placeholder]"
        await type_and_submit(page, input_sel, "button[data-testid='send-button']", full_prompt)

        print_step("ChatGPT", "Waiting for response...")

        # Wait for stop button to appear (generation started) then disappear (done)
        try:
            await page.wait_for_selector("button[data-testid='stop-button']", timeout=15000)
            await page.wait_for_selector("button[data-testid='stop-button']", state="hidden", timeout=RESPONSE_TIMEOUT * 1000)
        except Exception:
            pass

        await asyncio.sleep(SETTLE_WAIT)

        # Scrape the last assistant message
        response_sel = "[data-message-author-role='assistant'] .markdown, [data-message-author-role='assistant'] p"
        elements = await page.query_selector_all(response_sel)
        texts = [await el.inner_text() for el in elements if el]
        result = "\n".join(texts).strip()

        if not result:
            # Fallback: grab all visible assistant turns
            result = await wait_for_typing_to_stop(page, "[data-message-author-role='assistant']")

        return result or "[ChatGPT: no response captured]"

    except Exception as e:
        return f"[ChatGPT error: {e}]"
    finally:
        await page.close()


async def ask_gemini(context: BrowserContext, question: str) -> str:
    page = await context.new_page()
    try:
        print_step("Gemini", "Opening page...")
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        full_prompt = f"{GEMINI_ROLE}\n\nQuestion: {question}"

        input_sel = "rich-textarea div[contenteditable='true'], textarea.message-input, [aria-label*='message' i][contenteditable]"
        await type_and_submit(page, input_sel, "button[aria-label*='Send' i], button[data-test-id='send-button']", full_prompt)

        print_step("Gemini", "Waiting for response...")

        # Gemini shows a loading spinner; wait for it to disappear
        try:
            await page.wait_for_selector(".loading-indicator, [aria-label*='loading' i]", timeout=10000)
            await page.wait_for_selector(".loading-indicator, [aria-label*='loading' i]", state="hidden", timeout=RESPONSE_TIMEOUT * 1000)
        except Exception:
            pass

        await asyncio.sleep(SETTLE_WAIT)

        response_sel = "model-response .response-content, .model-response-text, message-content"
        result = await wait_for_typing_to_stop(page, response_sel)

        return result or "[Gemini: no response captured]"

    except Exception as e:
        return f"[Gemini error: {e}]"
    finally:
        await page.close()


async def ask_claude(context: BrowserContext, question: str, role: str = "") -> str:
    page = await context.new_page()
    try:
        print_step("Claude", "Opening page...")
        await page.goto(CLAUDE_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        full_prompt = f"{role}\n\nQuestion: {question}" if role else question

        input_sel = '[contenteditable="true"].ProseMirror, div[contenteditable="true"][data-placeholder], div.ProseMirror'
        await type_and_submit(page, input_sel, 'button[aria-label="Send message"]', full_prompt)

        print_step("Claude", "Waiting for response...")

        # Claude shows a stop button while generating
        try:
            await page.wait_for_selector('button[aria-label="Stop response"]', timeout=15000)
            await page.wait_for_selector('button[aria-label="Stop response"]', state="hidden", timeout=RESPONSE_TIMEOUT * 1000)
        except Exception:
            pass

        await asyncio.sleep(SETTLE_WAIT)

        response_sel = ".font-claude-message, [data-is-streaming='false'] .prose, .message-content"
        result = await wait_for_typing_to_stop(page, response_sel)

        return result or "[Claude: no response captured]"

    except Exception as e:
        return f"[Claude error: {e}]"
    finally:
        await page.close()


async def synthesize_with_claude(context: BrowserContext, question: str, r_chatgpt: str, r_gemini: str, r_claude: str) -> str:
    """Uses a fresh Claude tab as the 4th AI to synthesize all three responses."""
    page = await context.new_page()
    try:
        print_step("Claude (Synthesizer)", "Opening page for final synthesis...")
        await page.goto(CLAUDE_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        synth_prompt = SYNTHESIZER_PROMPT.format(
            question=question,
            response_chatgpt=r_chatgpt,
            response_gemini=r_gemini,
            response_claude=r_claude,
        )

        input_sel = '[contenteditable="true"].ProseMirror, div[contenteditable="true"][data-placeholder], div.ProseMirror'
        await type_and_submit(page, input_sel, 'button[aria-label="Send message"]', synth_prompt)

        print_step("Claude (Synthesizer)", "Synthesizing final answer...")

        try:
            await page.wait_for_selector('button[aria-label="Stop response"]', timeout=15000)
            await page.wait_for_selector('button[aria-label="Stop response"]', state="hidden", timeout=RESPONSE_TIMEOUT * 1000)
        except Exception:
            pass

        await asyncio.sleep(SETTLE_WAIT)

        response_sel = ".font-claude-message, [data-is-streaming='false'] .prose, .message-content"
        result = await wait_for_typing_to_stop(page, response_sel)

        return result or "[Synthesizer: no response captured]"

    except Exception as e:
        return f"[Synthesizer error: {e}]"
    finally:
        await page.close()


# ─────────────────────────────────────────────
#  MAIN ORCHESTRATOR
# ─────────────────────────────────────────────

async def orchestrate(question: str, headless: bool = True):
    print_banner()
    print(f"\n  Question: {textwrap.fill(question, 56, subsequent_indent='            ')}\n")

    async with async_playwright() as pw:
        # Use your real Chrome profile so you're already logged in
        # Change this path to your actual Chrome user data directory:
        #   macOS:   ~/Library/Application Support/Google/Chrome
        #   Windows: C:\Users\YOU\AppData\Local\Google\Chrome\User Data
        #   Linux:   ~/.config/google-chrome
        user_data_dir = Path.home() / "Library/Application Support/Google/Chrome"

        print_step("Browser", f"Launching with profile: {user_data_dir}")

        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            channel="chrome",          # uses installed Chrome, not bundled Chromium
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )

        # ── Step 1: Ask all three AIs in parallel ──
        print_step("Pipeline", "Querying ChatGPT, Gemini, and Claude in parallel...")

        results = await asyncio.gather(
            ask_chatgpt(context, question),
            ask_gemini(context, question),
            ask_claude(context, question, CLAUDE_ROLE),
            return_exceptions=True
        )

        r_chatgpt = str(results[0]) if not isinstance(results[0], Exception) else f"[Error: {results[0]}]"
        r_gemini  = str(results[1]) if not isinstance(results[1], Exception) else f"[Error: {results[1]}]"
        r_claude  = str(results[2]) if not isinstance(results[2], Exception) else f"[Error: {results[2]}]"

        print_response("ChatGPT (Researcher)", r_chatgpt)
        print_response("Gemini (Devil's Advocate)", r_gemini)
        print_response("Claude (Specialist)", r_claude)

        # ── Step 2: Synthesize with a 4th Claude instance ──
        print_step("Pipeline", "Sending all three responses to Claude (Synthesizer)...")

        final_answer = await synthesize_with_claude(context, question, r_chatgpt, r_gemini, r_claude)

        await context.close()

    # ── Output ──
    print("\n" + "═" * 60)
    print("  ✅  FINAL SYNTHESIZED ANSWER")
    print("═" * 60)
    print(f"\n{final_answer}\n")

    # Save to file
    out_file = Path("answer.txt")
    out_file.write_text(
        f"Question: {question}\n\n"
        f"{'='*60}\nChatGPT (Researcher)\n{'='*60}\n{r_chatgpt}\n\n"
        f"{'='*60}\nGemini (Devil's Advocate)\n{'='*60}\n{r_gemini}\n\n"
        f"{'='*60}\nClaude (Specialist)\n{'='*60}\n{r_claude}\n\n"
        f"{'='*60}\nFINAL SYNTHESIZED ANSWER\n{'='*60}\n{final_answer}\n",
        encoding="utf-8"
    )
    print(f"  💾  Saved to: {out_file.resolve()}\n")

    return final_answer


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-AI orchestration pipeline via browser automation")
    parser.add_argument("--question", "-q", type=str, help="Question to ask all AIs")
    parser.add_argument("--headed", action="store_true", help="Show browser windows (useful for debugging)")
    args = parser.parse_args()

    if args.question:
        question = args.question
    else:
        print("\n  Enter your question (press Enter twice when done):")
        lines = []
        while True:
            line = input()
            if line == "" and lines:
                break
            lines.append(line)
        question = " ".join(lines).strip()

    if not question:
        print("No question provided. Exiting.")
        sys.exit(1)

    asyncio.run(orchestrate(question, headless=not args.headed))


if __name__ == "__main__":
    main()
