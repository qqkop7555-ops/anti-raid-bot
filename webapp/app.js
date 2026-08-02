(() => {
  const tg = window.Telegram?.WebApp;
  tg?.ready();
  tg?.expand();

  const $ = (id) => document.getElementById(id);
  const show = (id) => {
    ["loading", "challenge", "success", "error"].forEach((s) => $(s).classList.add("hidden"));
    $(id).classList.remove("hidden");
  };

  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") || tg?.initDataUnsafe?.start_param || "";
  const initData = tg?.initData || "";

  let answered = false;

  async function loadChallenge() {
    if (!token) {
      $("errorText").textContent = "Ссылка без токена — открой капчу через кнопку в боте.";
      show("error");
      return;
    }
    try {
      const res = await fetch(`/api/challenge?token=${encodeURIComponent(token)}`);
      if (!res.ok) throw new Error("expired");
      const data = await res.json();

      $("chatTitle").textContent = data.chat_title ? `Чат: ${data.chat_title}` : "";
      $("question").textContent = data.question;

      const optionsEl = $("options");
      optionsEl.innerHTML = "";
      data.options.forEach((opt) => {
        const btn = document.createElement("button");
        btn.className = "opt";
        btn.textContent = opt;
        btn.onclick = () => submitAnswer(opt, btn);
        optionsEl.appendChild(btn);
      });

      show("challenge");
    } catch (e) {
      $("errorText").textContent = "Проверка устарела или уже пройдена. Вернись в бота и попробуй заново.";
      show("error");
    }
  }

  async function submitAnswer(answer, btnEl) {
    if (answered) return;
    answered = true;
    document.querySelectorAll(".opt").forEach((b) => (b.disabled = true));
    btnEl.classList.add("selected");
    tg?.HapticFeedback?.impactOccurred("light");

    try {
      const res = await fetch("/api/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, answer, init_data: initData }),
      });
      const data = await res.json();

      if (!res.ok || !data.ok) {
        if (data.reason === "wrong_answer") {
          btnEl.classList.remove("selected");
          btnEl.classList.add("wrong");
          answered = false;
          document.querySelectorAll(".opt").forEach((b) => (b.disabled = false));
          tg?.HapticFeedback?.notificationOccurred("error");
          return;
        }
        $("errorText").textContent = data.message || "Не получилось подтвердить проверку.";
        show("error");
        return;
      }

      tg?.HapticFeedback?.notificationOccurred("success");
      $("successText").textContent = data.message || "Проверка пройдена.";
      const link = $("actionLink");
      if (data.invite_link) {
        link.href = data.invite_link;
        link.textContent = "🎉 Открыть канал";
        link.classList.remove("hidden");
      } else {
        link.classList.add("hidden");
      }
      show("success");
      setTimeout(() => tg?.close(), data.invite_link ? 60000 : 1800);
    } catch (e) {
      $("errorText").textContent = "Нет связи с сервером. Попробуй ещё раз.";
      show("error");
      answered = false;
    }
  }

  loadChallenge();
})();
