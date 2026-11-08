"use strict";

const form = document.querySelector("#login-form");
const message = document.querySelector("#form-message");

if (
  message instanceof HTMLElement &&
  new URLSearchParams(window.location.search).get("registered") === "1"
) {
  message.textContent = "Аккаунт создан. Теперь войдите в игру.";
  message.dataset.kind = "success";
  message.hidden = false;
}

if (form instanceof HTMLFormElement) {
  const emailInput = document.querySelector("#email");
  const passwordInput = document.querySelector("#password");
  const submitButton = document.querySelector("#submit-button");
  const buttonLabel = submitButton?.querySelector(".button-label");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    if (
      !(emailInput instanceof HTMLInputElement) ||
      !(passwordInput instanceof HTMLInputElement) ||
      !(submitButton instanceof HTMLButtonElement) ||
      !(message instanceof HTMLElement) ||
      !(buttonLabel instanceof HTMLElement)
    ) {
      return;
    }

    message.hidden = true;
    message.textContent = "";
    message.dataset.kind = "";
    submitButton.disabled = true;
    submitButton.setAttribute("aria-busy", "true");
    buttonLabel.textContent = "Входим…";

    try {
      const response = await fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: emailInput.value.trim(),
          password: passwordInput.value,
        }),
      });

      if (!response.ok) {
        message.textContent =
          response.status === 401
            ? "Не удалось войти. Проверьте электронную почту и пароль."
            : "Сервис временно недоступен. Попробуйте ещё раз позже.";
        message.hidden = false;
        return;
      }

      window.location.assign("/game/");
    } catch {
      message.textContent = "Не удалось связаться с сервером. Проверьте подключение.";
      message.hidden = false;
    } finally {
      passwordInput.value = "";
      submitButton.disabled = false;
      submitButton.removeAttribute("aria-busy");
      buttonLabel.textContent = "Войти";
    }
  });
}
