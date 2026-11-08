"use strict";

const form = document.querySelector("#register-form");

if (form instanceof HTMLFormElement) {
  const usernameInput = document.querySelector("#username");
  const emailInput = document.querySelector("#email");
  const passwordInput = document.querySelector("#password");
  const confirmationInput = document.querySelector("#password-confirmation");
  const submitButton = document.querySelector("#submit-button");
  const message = document.querySelector("#form-message");
  const buttonLabel = submitButton?.querySelector(".button-label");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    if (
      !(usernameInput instanceof HTMLInputElement) ||
      !(emailInput instanceof HTMLInputElement) ||
      !(passwordInput instanceof HTMLInputElement) ||
      !(confirmationInput instanceof HTMLInputElement) ||
      !(submitButton instanceof HTMLButtonElement) ||
      !(message instanceof HTMLElement) ||
      !(buttonLabel instanceof HTMLElement)
    ) {
      return;
    }

    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    const confirmation = confirmationInput.value;

    message.hidden = true;
    message.textContent = "";
    message.dataset.kind = "";

    if (!username || username.length > 32) {
      message.textContent = "Имя пользователя должно содержать от 1 до 32 символов.";
      message.hidden = false;
      usernameInput.focus();
      return;
    }

    if (new TextEncoder().encode(password).length > 72) {
      message.textContent = "Пароль не должен превышать 72 байта в кодировке UTF-8.";
      message.hidden = false;
      passwordInput.focus();
      return;
    }

    if (password !== confirmation) {
      message.textContent = "Пароли не совпадают.";
      message.hidden = false;
      confirmationInput.focus();
      return;
    }

    submitButton.disabled = true;
    submitButton.setAttribute("aria-busy", "true");
    buttonLabel.textContent = "Создаём аккаунт…";

    try {
      const response = await fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          email: emailInput.value.trim(),
          password,
        }),
      });

      if (!response.ok) {
        if (response.status === 409) {
          message.textContent = "Это имя пользователя или электронная почта уже заняты.";
        } else if (response.status === 422) {
          message.textContent = "Проверьте введённые данные и попробуйте ещё раз.";
        } else {
          message.textContent = "Не удалось создать аккаунт. Попробуйте ещё раз позже.";
        }
        message.hidden = false;
        return;
      }

      window.location.assign("/login/?registered=1");
    } catch {
      message.textContent = "Не удалось связаться с сервером. Проверьте подключение.";
      message.hidden = false;
    } finally {
      passwordInput.value = "";
      confirmationInput.value = "";
      submitButton.disabled = false;
      submitButton.removeAttribute("aria-busy");
      buttonLabel.textContent = "Зарегистрироваться";
    }
  });
}
