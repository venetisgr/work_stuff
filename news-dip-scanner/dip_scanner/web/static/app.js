/* Dip scanner: small niceties on top of pages that work without JavaScript. No inline handlers anywhere (the
   Content-Security-Policy forbids them); pages opt in with data-attributes:

   data-copy="#selector"        on a button: copies the value (or text) of that element, then says "Copied"
   data-confirm="Question?"     on a form or a submit button: asks before submitting
   data-auto-submit             on a select, checkbox or radio inside a form: submits the form when it changes
                                (give that form's submit button class="no-js-only": it is hidden when this file runs)
   data-select-on-focus         on an input: selects its text when focused (links to copy by hand) */

(function () {
  "use strict";

  document.documentElement.classList.add("js");

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.className = "visually-hidden";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy") ? resolve() : reject(new Error("copy failed"));
      } catch (error) {
        reject(error);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) {
      return;
    }
    var target = document.querySelector(button.getAttribute("data-copy"));
    if (!target) {
      return;
    }
    event.preventDefault();
    var text = "value" in target ? target.value : target.textContent;
    var label = button.textContent;
    copyText(text.trim()).then(
      function () {
        button.textContent = "Copied";
        window.setTimeout(function () {
          button.textContent = label;
        }, 1800);
      },
      function () {
        if (typeof target.select === "function") {
          target.select();
        }
      }
    );
  });

  document.addEventListener(
    "submit",
    function (event) {
      var form = event.target;
      var submitter = event.submitter;
      var question =
        (submitter && submitter.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
      if (question && !window.confirm(question)) {
        event.preventDefault();
      }
    },
    true
  );

  document.addEventListener("change", function (event) {
    var field = event.target;
    if (!field.hasAttribute || !field.hasAttribute("data-auto-submit") || !field.form) {
      return;
    }
    if (typeof field.form.requestSubmit === "function") {
      field.form.requestSubmit();
    } else {
      field.form.submit();
    }
  });

  document.addEventListener("focusin", function (event) {
    var field = event.target;
    if (field.hasAttribute && field.hasAttribute("data-select-on-focus") && typeof field.select === "function") {
      window.setTimeout(function () {
        field.select();
      }, 0);
    }
  });

  // Close an open <details class="menu"> when clicking elsewhere or pressing Escape.
  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) {
        menu.removeAttribute("open");
      }
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      document.querySelectorAll("details.menu[open]").forEach(function (menu) {
        menu.removeAttribute("open");
      });
    }
  });
})();
