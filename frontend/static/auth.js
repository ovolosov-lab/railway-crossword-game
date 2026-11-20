"use strict";

const localization = JSON.parse(
  document.getElementById("i18n-data")?.textContent || "{}",
);
const messages = localization.js || {};
const t = (key) => messages[key] || key;
const form = document.querySelector("#login-form");
const message = document.querySelector("#form-message");

if (
  message instanceof HTMLElement &&
  new URLSearchParams(window.location.search).get("registered") === "1"
) {
  message.textContent = t("registered_message");
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
    buttonLabel.textContent = t("login_submitting");

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
            ? t("login_failed")
            : t("temporary_unavailable");
        message.hidden = false;
        return;
      }

      window.location.assign("/game/");
    } catch {
      message.textContent = t("connection_failed");
      message.hidden = false;
    } finally {
      passwordInput.value = "";
      submitButton.disabled = false;
      submitButton.removeAttribute("aria-busy");
      buttonLabel.textContent = t("login_submit");
    }
  });
}
