/* VELA AI Assistant — drop-in widget.
   Embed:
   <script src="https://YOURHOST/static/widget.js"
           data-api="https://YOURHOST" data-key="TENANT_KEY" data-name="Your Business"></script>
   NOTE: for production, proxy the key server-side (see SECURITY.md). */
(function () {
  // Guard: only initialise once even if the script is injected twice (SPA nav / re-embed).
  if (window.__VELA_ASSISTANT_LOADED__) return;
  window.__VELA_ASSISTANT_LOADED__ = true;

  var s = document.currentScript;                 // null when injected dynamically (React/SPA)
  var cfg = window.__VELA_ASSISTANT__ || {};       // fallback config for dynamic embeds
  function opt(attr, key, def) {
    return (s && s.getAttribute(attr)) || cfg[key] || def;
  }
  var API = opt("data-api", "api", location.origin);
  var KEY = opt("data-key", "key", "demo-secret-change-me");
  // Preferred auth: a PUBLIC, read-only, origin-locked widget token (wt_...) issued by the
  // admin portal. When present the widget calls the /widget/* proxy with X-Widget-Token and
  // never exposes the write-capable tenant key (closes audit finding H1).
  var WIDGET_TOKEN = opt("data-widget-token", "widget_token", "");
  var NAME = opt("data-name", "name", "Assistant");
  var ACCENT = opt("data-accent", "accent", "#c8a24a");
  var BOOK = opt("data-book", "book", "https://byvela.online/book");
  var PHONE = opt("data-phone", "phone", "+1 430 279 9571");
  var TEL = "tel:" + PHONE.replace(/[^\d+]/g, "");
  var POS = opt("data-pos", "pos", "right");       // "right" (default) or "left"
  var SIDE = POS === "left" ? "left:20px" : "right:20px";

  var css = `
  .va-btn{position:fixed;bottom:20px;${SIDE};width:56px;height:56px;border-radius:50%;
    background:${ACCENT};color:#111;border:none;cursor:pointer;font-size:24px;z-index:99998;
    box-shadow:0 8px 24px rgba(0,0,0,.25)}
  .va-panel{position:fixed;bottom:88px;${SIDE};width:360px;max-width:92vw;height:520px;max-height:74vh;
    background:#0f0f10;color:#eee;border:1px solid #2a2a2c;border-radius:16px;display:none;
    flex-direction:column;overflow:hidden;z-index:99999;font-family:system-ui,-apple-system,sans-serif;
    box-shadow:0 20px 60px rgba(0,0,0,.5)}
  .va-panel.open{display:flex}
  .va-hd{padding:14px 16px;border-bottom:1px solid #2a2a2c;font-weight:600}
  .va-hd small{display:block;font-weight:400;color:#888;font-size:11px;margin-top:2px}
  .va-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
  .va-msg{padding:9px 12px;border-radius:12px;max-width:85%;font-size:14px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word}
  .va-u{align-self:flex-end;background:${ACCENT};color:#111}
  .va-a{align-self:flex-start;background:#1c1c1e;border:1px solid #2a2a2c}
  .va-cursor::after{content:"▍";opacity:.6;animation:va-blink 1s steps(2) infinite}
  @keyframes va-blink{50%{opacity:0}}
  .va-lead{margin:0}
  .va-kp{margin:8px 0 0;padding-left:18px;display:flex;flex-direction:column;gap:4px}
  .va-kp li{font-size:13px;line-height:1.4;color:#dcdcdc}
  .va-src{font-size:10px;color:#777;margin-top:6px}
  .va-act{align-self:flex-start;margin-top:2px}
  .va-act button{background:transparent;color:${ACCENT};border:1px solid ${ACCENT};
    border-radius:999px;padding:5px 12px;font-size:12px;cursor:pointer;font-weight:600}
  .va-cta{align-self:flex-start;display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
  .va-cta a{display:inline-flex;align-items:center;gap:6px;text-decoration:none;font-size:12px;
    font-weight:600;border-radius:999px;padding:7px 13px;border:1px solid ${ACCENT}}
  .va-cta a.va-book{background:${ACCENT};color:#111}
  .va-cta a.va-phone{background:transparent;color:${ACCENT}}
  .va-hand{align-self:flex-start;font-size:11px;color:#c88;margin-top:2px}
  .va-typing{display:flex;gap:5px;align-items:center;padding:13px 14px}
  .va-dot{width:7px;height:7px;border-radius:50%;background:#8a8a8a;animation:va-bounce 1.3s infinite ease-in-out}
  .va-dot:nth-child(2){animation-delay:.16s}
  .va-dot:nth-child(3){animation-delay:.32s}
  @keyframes va-bounce{0%,70%,100%{transform:translateY(0);opacity:.45}35%{transform:translateY(-6px);opacity:1}}
  .va-in{display:flex;gap:8px;padding:12px;border-top:1px solid #2a2a2c}
  .va-in input{flex:1;background:#1c1c1e;border:1px solid #2a2a2c;color:#eee;border-radius:10px;padding:9px 12px;font-size:14px;outline:none}
  .va-in button{background:${ACCENT};color:#111;border:none;border-radius:10px;padding:0 14px;cursor:pointer;font-weight:600}`;
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  var btn = document.createElement("button"); btn.className = "va-btn"; btn.innerHTML = "&#128172;";
  var panel = document.createElement("div"); panel.className = "va-panel";
  panel.innerHTML =
    '<div class="va-hd">' + NAME + '<small>AI assistant · answers from ' + NAME + "'s info</small></div>" +
    '<div class="va-log"></div>' +
    '<div class="va-in"><input placeholder="Ask a question..." /><button>Send</button></div>';
  document.body.appendChild(btn); document.body.appendChild(panel);

  var log = panel.querySelector(".va-log");
  var input = panel.querySelector("input");
  var send = panel.querySelector("button");

  function add(text, who, sources) {
    var d = document.createElement("div"); d.className = "va-msg " + (who === "u" ? "va-u" : "va-a");
    d.textContent = text;
    if (sources && sources.length) {
      var sd = document.createElement("div"); sd.className = "va-src";
      sd.textContent = "Source: " + sources.slice(0, 2).join(", "); d.appendChild(sd);
    }
    log.appendChild(d); log.scrollTop = log.scrollHeight;
  }

  btn.onclick = function () {
    panel.classList.toggle("open");
    if (panel.classList.contains("open") && !log.children.length)
      add("Hi! Ask me anything about " + NAME + ".", "a");
  };

  // Create an empty assistant bubble we stream text into.
  function addStreaming() {
    var d = document.createElement("div");
    d.className = "va-msg va-a va-cursor";
    var body = document.createElement("span");
    d.appendChild(body);
    log.appendChild(d); log.scrollTop = log.scrollHeight;
    return { wrap: d, body: body };
  }
  function setStreamText(b, text) {
    b.body.textContent = text; log.scrollTop = log.scrollHeight;
  }
  // On the final `result` event, drop the cursor and RE-RENDER the reply cleanly:
  // a short answer paragraph + a real bulleted list (instead of raw streamed "- " text).
  function finalizeStream(b, result) {
    b.wrap.classList.remove("va-cursor");
    if (!result) { if (!b.body.textContent) b.body.textContent = "Sorry, something went wrong."; return; }
    b.body.textContent = "";
    b.wrap.style.whiteSpace = "normal";
    var lead = document.createElement("p"); lead.className = "va-lead";
    lead.textContent = result.answer || "Sorry, something went wrong.";
    b.body.appendChild(lead);
    if (result.key_points && result.key_points.length) {
      var ul = document.createElement("ul"); ul.className = "va-kp";
      result.key_points.forEach(function (p) {
        var li = document.createElement("li"); li.textContent = p; ul.appendChild(li);
      });
      b.body.appendChild(ul);
    }
    if (result.sources && result.sources.length && !result.needs_human) {
      var sd = document.createElement("div"); sd.className = "va-src";
      sd.textContent = "Source: " + result.sources.slice(0, 2).join(", ");
      b.wrap.appendChild(sd);
    }
    if (result.needs_human) {
      var hd = document.createElement("div"); hd.className = "va-hand";
      hd.textContent = "Not sure? A team member can help.";
      log.appendChild(hd);
    }
    // "Book a call" or any human hand-off -> show the real booking link + phone number.
    if (result.needs_human || result.suggested_action === "Book a call" || result.suggested_action === "Talk to the team") {
      var cta = document.createElement("div"); cta.className = "va-cta";
      var bk = document.createElement("a"); bk.className = "va-book";
      bk.href = BOOK; bk.target = "_blank"; bk.rel = "noopener";
      bk.textContent = "📅 Book a call";
      var ph = document.createElement("a"); ph.className = "va-phone";
      ph.href = TEL; ph.textContent = "📞 Call " + PHONE;
      cta.appendChild(bk); cta.appendChild(ph);
      log.appendChild(cta);
    } else if (result.suggested_action) {
      var ad = document.createElement("div"); ad.className = "va-act";
      var ab = document.createElement("button"); ab.textContent = result.suggested_action;
      ab.onclick = function () { input.value = result.suggested_action + " — "; input.focus(); };
      ad.appendChild(ab); log.appendChild(ad);
    }
    log.scrollTop = log.scrollHeight;
  }
  function parseSSE(block) {
    var ev = null, data = "";
    block.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) ev = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
    });
    if (!ev) return null;
    try { return { event: ev, data: data ? JSON.parse(data) : {} }; } catch (e) { return null; }
  }

  // Animated three-dot "typing" indicator shown until the first token arrives.
  function typingDots() {
    var d = document.createElement("div");
    d.className = "va-msg va-a va-typing";
    d.innerHTML = '<span class="va-dot"></span><span class="va-dot"></span><span class="va-dot"></span>';
    log.appendChild(d); log.scrollTop = log.scrollHeight;
    return d;
  }

  function ask() {
    var q = input.value.trim(); if (!q) return;
    add(q, "u"); input.value = "";
    var dots = typingDots();
    var bubble = null, target = "", shown = 0, pending = null, streamDone = false, timer = null;

    // Reveal buffered text char-by-char (typewriter), independent of network chunk size.
    function tick() {
      if (shown < target.length) {
        var step = Math.max(2, Math.ceil((target.length - shown) / 14)); // ease-out, min 2 chars/frame
        shown = Math.min(target.length, shown + step);
        setStreamText(bubble, target.slice(0, shown));
      } else if (streamDone) {
        clearInterval(timer); timer = null;
        finalizeStream(bubble, pending); // re-render into clean answer + bullets
      }
    }
    function startBubble() {
      if (bubble) return;
      if (dots) { dots.remove(); dots = null; }
      bubble = addStreaming();
      timer = setInterval(tick, 18);
    }
    function finish() {
      streamDone = true;
      if (!bubble) { startBubble(); target = (pending && pending.answer) || "Sorry, something went wrong."; }
    }

    var endpoint = WIDGET_TOKEN ? "/widget/chat/stream" : "/chat/stream";
    var headers = { "Content-Type": "application/json" };
    if (WIDGET_TOKEN) headers["X-Widget-Token"] = WIDGET_TOKEN; else headers["X-API-Key"] = KEY;
    fetch(API + endpoint, {
      method: "POST", headers: headers,
      body: JSON.stringify({ message: q })
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("bad response");
      var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { finish(); return; }
          buf += dec.decode(res.value, { stream: true });
          var parts = buf.split("\n\n"); buf = parts.pop();
          parts.forEach(function (block) {
            var e = parseSSE(block); if (!e) return;
            if (e.event === "token" && e.data.text) { startBubble(); target += e.data.text; }
            else if (e.event === "result") { pending = e.data; }
          });
          return pump();
        });
      }
      return pump();
    }).catch(function () {
      if (timer) { clearInterval(timer); timer = null; }
      if (dots) { dots.remove(); dots = null; }
      if (!bubble) bubble = addStreaming();
      bubble.wrap.classList.remove("va-cursor");
      setStreamText(bubble, "Sorry, I couldn't reach the server.");
    });
  }
  send.onclick = ask;
  input.addEventListener("keydown", function (e) { if (e.key === "Enter") ask(); });
})();
