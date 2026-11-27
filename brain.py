import threading
from datetime import datetime

import llm
from db import (
    setup_database, save_message, load_recent_messages,
    get_user_profile, load_setting
)
from memory import (
    rebuild_faiss_index, search_similar_memories, save_embedding
)
from search import web_search


# Two embedding jobs can start at the same time (your message + PERI's reply).
# The lock makes them take turns so FAISS and vector_id_map never drift apart.
_embed_lock = threading.Lock()


def _embed(text, message_id):
    with _embed_lock:
        save_embedding(text, message_id)


class Brain:

    def __init__(self):
        # Startup work: runs once, when the first message is sent
        setup_database()
        llm.load_saved_provider_and_model()
        rebuild_faiss_index()

        self.conversation_history = load_recent_messages(20)
        self.conversation_summary = ""

    # ── Query classification ──────────────────────────

    def classify_query(self, text):
        text = text.lower()
        words = text.split()

        if any(x in text for x in ["who won", "latest", "news", "today", "score"]):
            return "FACTUAL"
        if any(x in text for x in ["remember", "you said", "last time", "earlier"]):
            return "MEMORY"
        # whole-word match, so "this" or "which" no longer counts as "hi"
        if any(w in ("hi", "hello", "hey") for w in words) or "how are you" in text:
            return "CASUAL"
        return "GENERAL"

    # ── Entry point (called by the UI) ────────────────

    def respond(self, user_input, on_reply, on_error):
        self.conversation_history.append({
            "role": "user",
            "content": user_input
        })
        threading.Thread(
            target=self._generate,
            args=(user_input, on_reply, on_error)
        ).start()

    # ── The actual work (runs in a background thread) ─

    def _generate(self, user_message, on_reply, on_error):
        try:
            query_type = self.classify_query(user_message)
            use_memory = query_type in ("MEMORY", "GENERAL")
            use_search = (
                query_type == "FACTUAL"
                and bool(load_setting("web_search_api_key", ""))
            )

            memories = search_similar_memories(user_message) if use_memory else []
            search_results = web_search(user_message) if use_search else None

            system_prompt = self._build_prompt(query_type, search_results, memories)

            messages = [{"role": "system", "content": system_prompt}]
            if self.conversation_summary:
                messages.append({
                    "role": "system",
                    "content": f"Summary: {self.conversation_summary}"
                })
            messages += self.conversation_history[-8:]

            reply = llm.chat_completion(model=llm.CURRENT_MODEL, messages=messages)

            # try/except only catches crashes. These two checks catch
            # replies that are technically "successful" but useless.
            if not reply or not reply.strip():
                on_error("Empty response received")
                return
            if reply.startswith("[Error]"):
                on_error(reply)
                return

            self.conversation_history.append({
                "role": "assistant",
                "content": reply
            })

            # Save both sides of the exchange. _embed is handed to save_message
            # so db.py never has to know FAISS exists.
            save_message("user", user_message, on_embedding=_embed)
            save_message("assistant", reply, on_embedding=_embed)

            # Show the reply first, then do housekeeping
            on_reply(reply)

            try:
                self._maybe_extract_profile()
                self._maybe_summarize()
            except Exception as e:
                print(f"[Brain] background task failed: {e}")

        except Exception as e:
            on_error(str(e))

    # ── Prompt building ───────────────────────────────

    def _build_prompt(self, query_type, search_results, memories):
        prompt = """
You are PERI, a personal companion. Calm, direct, genuine.
- No bullet points in casual conversation
- Never ask more than one question at a time
- Match the user's energy
- Keep responses short unless detail is requested
- Never say "Great!", "Certainly!", "Of course!"
"""
        prompt += f"\nCurrent time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"

        if search_results:
            prompt += f"\nWeb results:\n{search_results}\n"
        elif query_type == "FACTUAL":
            prompt += "\nNo web search available. Say so if uncertain.\n"

        user_profile = get_user_profile()
        if user_profile:
            prompt += f"\nUser background (use naturally):\n{user_profile}\n"

        if memories:
            prompt += "\nRelevant memories:\n"
            for m in memories:
                prompt += f"{m['role']}: {m['text']}\n"

        return prompt

    # ── Background housekeeping ───────────────────────

    def _maybe_extract_profile(self):
        if len(self.conversation_history) % 8 == 0:
            snippet = self.conversation_history[-8:]
            snippet_text = "\n".join(
                f"{m['role']}: {m['content']}" for m in snippet
            )
            threading.Thread(
                target=llm.extract_and_save_profile,
                args=(snippet_text,)
            ).start()

    def _maybe_summarize(self):
        if len(self.conversation_history) > 20:
            summary_prompt = f"Summarize briefly:\n{self.conversation_history[:-10]}"
            summary = llm.chat_completion(
                model=llm.CURRENT_MODEL,
                messages=[{"role": "user", "content": summary_prompt}]
            )
            # don't store an error message as if it were a summary
            if summary and not summary.startswith("[Error]"):
                self.conversation_summary = summary
                self.conversation_history = self.conversation_history[-10:]
