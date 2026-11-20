"use strict";

const localization = JSON.parse(
  document.getElementById("i18n-data")?.textContent || "{}",
);
const messages = localization.js || {};
const t = (key) => messages[key] || key;
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
      message.textContent = t("username_invalid");
      message.hidden = false;
      usernameInput.focus();
      return;
    }

    if (new TextEncoder().encode(password).length > 72) {
      message.textContent = t("password_too_long");
      message.hidden = false;
      passwordInput.focus();
      return;
    }

    if (password !== confirmation) {
      message.textContent = t("password_mismatch");
      message.hidden = false;
      confirmationInput.focus();
      return;
    }

    submitButton.disabled = true;
    submitButton.setAttribute("aria-busy", "true");
    buttonLabel.textContent = t("register_submitting");

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
          message.textContent = t("register_conflict");
        } else if (response.status === 422) {
          message.textContent = t("validation_failed");
        } else {
          message.textContent = t("register_failed");
        }
        message.hidden = false;
        return;
      }

      window.location.assign("/login/?registered=1");
    } catch {
      message.textContent = t("connection_failed");
      message.hidden = false;
    } finally {
      passwordInput.value = "";
      confirmationInput.value = "";
      submitButton.disabled = false;
      submitButton.removeAttribute("aria-busy");
      buttonLabel.textContent = t("register_submit");
    }
  });
}
