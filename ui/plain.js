// Plain-English vocabulary shared by every page. Technical identifiers in, everyday words out.
window.PLAIN = (() => {
  const rule = {
    MISSING_SUBMISSION: "Monthly update is overdue",
    DUE_SOON: "Monthly update is due soon",
    LATE_SUBMISSION: "Update was submitted late",
    WEAK_BLOCKER: "Marked Red or Amber with no real explanation",
    STALE_BLOCKER: "Same problem reported again, no progress shown",
    PERSISTENT_RED: "Off track for several months in a row",
    CASH_RAG_INCONSISTENT: "Says “on track” but cash dropped sharply",
    SUSPICIOUS_CONTENT: "Unusual text in the update",
    NARRATIVE_RAG_MISMATCH: "Status doesn't match what the manager wrote",
    BLOCKER_QUALITY: "Explanation is too vague",
    DATA_ANOMALY: "Numbers look unusual",
  };
  const decision = {
    execute: "Done automatically", queue: "Waiting for you", suppressed: "Skipped (you made an exception)",
    duplicate: "Already handled earlier", shadow: "Practice only, not sent", log: "Noted, no action needed",
  };
  const action = {
    remind_pm: "Remind the portfolio manager", escalate_cos: "Alert you", flag_for_review: "Ask you to take a look",
    log_only: "Just note it",
  };
  const rag = { GREEN: "On track", AMBER: "At risk", RED: "Off track" };
  const severity = { LOW: "Low", MEDIUM: "Medium", HIGH: "High", CRITICAL: "Critical" };
  const emailKind = {
    remind_pm: "Reminder", escalate_cos: "Intervention alert", "remind_pm(approved)": "Reminder (you approved it)",
    "escalate_cos(approved)": "Alert (you approved it)", dispute_opened: "Dispute notice", dispute_decided: "Dispute outcome",
    settings_test: "Test email", auth_code: "Sign-in code", auth_alert: "Sign-in alert",
  };
  const delivery = { sent: "Sent", simulated: "Practice only (not actually sent)", failed: "Failed, will retry", console: "Shown on screen only" };
  const mode = {
    auto: ["Automatic", "Sends routine reminders by itself. Asks you before anything serious."],
    assist: ["Ask me first", "Nothing is sent until you approve it."],
    shadow: ["Practice", "Only shows what it would do. Nothing is sent."],
  };
  function why(reason) {
    const r = String(reason || "");
    const rules = [
      [/^judgement call at/, "The assistant isn't sure enough to act on its own, so it's asking you."],
      [/LLM-originated escalations/, "The assistant never alerts you about a portfolio on its own judgement without your OK."],
      [/explicitly needs a human/, "This needs a person to look at it."],
      [/minimum to act/, "The assistant is unsure, so it's asking you."],
      [/assist mode/, "You chose “Ask me first”, so every action waits for you."],
      [/disputes a deterministic rule/, "The AI disagreed with a standard rule, so you decide."],
      [/failed grounding/, "The AI's evidence didn't match the records, so it's not trusted."],
      [/no active PM/, "No portfolio manager is assigned to this portfolio."],
      [/no active CoS/, "There's no Chief of Staff account to send this to."],
      [/low-impact reminder/, "A standard rule was clearly broken, so a reminder went out automatically."],
      [/deterministic HIGH/, "A serious, clear-cut issue, so you were alerted automatically."],
      [/^confidence .* >= /, "The assistant was confident enough to send a reminder."],
      [/already sent/, "Already sent before, so it isn't repeated."],
      [/already waiting/, "Already waiting for your decision."],
      [/human override/, "You made an exception for this."],
      [/shadow mode/, "Practice mode: nothing is sent."],
      [/PAUSED/, "The assistant is paused."],
      [/informational/, "Information only."],
    ];
    for (const [re, txt] of rules) if (re.test(r)) return txt;
    return r;
  }
  function month(m) {
    if (!m) return "";
    const [y, mm] = String(m).split("-");
    return new Date(+y, +mm - 1, 1).toLocaleString("en-GB", { month: "long", year: "numeric" });
  }
  const sure = c => `${Math.round((+c || 0) * 100)}% sure`;
  function when(ts) {
    if (!ts) return "";
    const d = new Date(ts), now = new Date(), diff = (now - d) / 1000;
    const hm = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    if (diff < -60) {  // future (e.g. next scheduled check)
      if (d.toDateString() === now.toDateString()) return `today at ${hm}`;
      const t = new Date(now); t.setDate(now.getDate() + 1);
      if (d.toDateString() === t.toDateString()) return `tomorrow at ${hm}`;
      return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + ` at ${hm}`;
    }
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
    const sameDay = d.toDateString() === now.toDateString();
    const t = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    if (sameDay) return `today ${t}`;
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return `yesterday ${t}`;
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + " " + t;
  }
  const naira = v => (v < 0 ? "−" : "") + "₦" + Math.abs(+v).toLocaleString("en-NG", { maximumFractionDigits: 0 });
  return { rule: k => rule[k] || String(k || "").replace(/_/g, " ").toLowerCase(), decision: k => decision[k] || k,
           action: k => action[k] || k, rag: k => rag[k] || k, severity: k => severity[k] || k,
           emailKind: k => emailKind[k] || k, delivery: k => delivery[k] || k, mode, why, month, sure, when, naira };
})();
