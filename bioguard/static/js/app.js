/*
 * Progressive enhancement only.
 *
 * Every page is complete without this file: forms post normally, tables are
 * already ordered by the server, and the numbers beside each chart are rendered
 * server-side. What lives here is the stuff that is genuinely better with
 * JavaScript - drag and drop upload with per-file progress, column sorting,
 * tabs, instant filter boxes and dismissal of flash messages.
 */
(function () {
  "use strict";

  function on(root, event, selector, handler) {
    root.addEventListener(event, function (e) {
      var t = e.target && e.target.closest ? e.target.closest(selector) : null;
      if (t && root.contains(t)) handler(e, t);
    });
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function bytes(n) {
    if (n === undefined || n === null) return "";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  /* ------------------------------------------------------- flash messages */
  on(document, "click", ".flash-close", function (_e, btn) {
    var box = btn.closest(".flash");
    if (box) box.remove();
  });

  /* Auto-dismiss the quiet ones; keep warnings and errors up to be read. */
  window.setTimeout(function () {
    document.querySelectorAll(".flash-info, .flash-success").forEach(function (f) {
      f.style.transition = "opacity .5s";
      f.style.opacity = "0";
      window.setTimeout(function () { f.remove(); }, 600);
    });
  }, 9000);

  /* ------------------------------------------------------ profile dropdown */
  /* <details> already toggles with no JavaScript; this only adds the two
     conveniences of a real menu - dismiss on an outside click or on Escape. */
  var profile = document.getElementById("profile-menu");
  if (profile) {
    document.addEventListener("click", function (e) {
      if (profile.open && !profile.contains(e.target)) profile.open = false;
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && profile.open) {
        profile.open = false;
        var head = profile.querySelector("summary");
        if (head) head.focus();
      }
    });
  }

  /* -------------------------------------------------- destructive actions */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  /* ------------------------------------------------------------- sorting */
  /* The server already ordered every table meaningfully; this is a convenience
     re-sort of the rows currently on screen, never a new query. */
  function cellValue(row, index) {
    var cell = row.children[index];
    if (!cell) return "";
    if (cell.hasAttribute("data-v")) {
      var raw = cell.getAttribute("data-v");
      var asNum = parseFloat(raw);
      return String(asNum) === String(raw).trim() ? asNum : String(raw).toLowerCase();
    }
    var text = cell.textContent.trim();
    var num = text.replace(/[,\s%]+/g, "").match(/^-?\d+(\.\d+)?$/);
    return num ? parseFloat(num[0]) : text.toLowerCase();
  }

  on(document, "click", "table[data-sortable] thead th[data-sort] button", function (e, btn) {
    var th = btn.closest("th");
    var table = th.closest("table");
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var index = Array.prototype.indexOf.call(th.parentNode.children, th);
    var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
    table.querySelectorAll("thead th").forEach(function (h) { h.removeAttribute("aria-sort"); });
    th.setAttribute("aria-sort", dir);
    var asc = dir === "ascending";
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var av = cellValue(a, index), bv = cellValue(b, index);
      if (typeof av === "number" && typeof bv === "number") return asc ? av - bv : bv - av;
      av = String(av); bv = String(bv);
      if (av === bv) return 0;
      var cmp = av.localeCompare(bv);
      return asc ? cmp : -cmp;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
    e.preventDefault();
  });

  /* ------------------------------------------------- instant table filter */
  document.querySelectorAll("input[data-filter]").forEach(function (input) {
    var table = document.getElementById(input.getAttribute("data-filter"));
    if (!table || !table.tBodies[0]) return;
    var counter = document.querySelector('[data-filter-count="' + input.id + '"]');
    var total = table.tBodies[0].rows.length;
    input.addEventListener("input", function () {
      var needle = input.value.trim().toLowerCase();
      var shown = 0;
      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        var hit = !needle || row.textContent.toLowerCase().indexOf(needle) !== -1;
        row.hidden = !hit;
        if (hit) shown++;
      });
      if (counter) {
        counter.textContent = needle
          ? shown + " of " + total + " rows on this page match"
          : total + " rows on this page";
      }
    });
  });

  /* ---------------------------------------------------------------- tabs */
  on(document, "click", ".tabs button[data-tab]", function (_e, btn) {
    var group = btn.closest(".tabs");
    var target = document.getElementById(btn.getAttribute("data-tab"));
    if (!target || !group) return;
    group.querySelectorAll("button").forEach(function (b) {
      b.setAttribute("aria-selected", b === btn ? "true" : "false");
    });
    var panels = (group.parentNode || document).querySelectorAll(".tabpanel");
    Array.prototype.forEach.call(panels, function (p) { p.hidden = p !== target; });
  });

  /* Reveal a tab when linked directly, e.g. /reports/4#tab-parse-log. */
  function followHash() {
    var id = decodeURIComponent(window.location.hash || "").replace("#", "");
    if (!id) return;
    var btn = document.querySelector('.tabs button[data-tab="' + id + '"]');
    if (btn) btn.click();
  }
  window.addEventListener("hashchange", followHash);

  /* --------------------------------------------------------------- upload */
  /* One request per file: the API accepts a batch, but a per-file progress bar
     and a per-file verdict is what a lab manager actually reads. */
  var dropzone = document.getElementById("dropzone");
  if (dropzone) initUpload(dropzone);

  function initUpload(zone) {
    var picker = zone.querySelector("input[type=file]");
    var listEl = document.getElementById("filelist");
    var sendBtn = document.getElementById("send");
    var statusEl = document.getElementById("upload-status");
    var dupBox = document.getElementById("allow_duplicate");
    var allowed = (zone.getAttribute("data-allowed") || ".csv,.tsv,.txt,.pdf").split(",");
    var maxBytes = parseFloat(zone.getAttribute("data-max-mb") || "25") * 1048576;
    var queue = [];
    var imported = 0;

    function rejectReason(file) {
      var name = (file.name || "").toLowerCase();
      var known = allowed.some(function (a) {
        var ext = a.trim().toLowerCase();
        return ext && name.slice(-ext.length) === ext;
      });
      if (!known) return "Unsupported type - expected " + allowed.join(", ");
      if (file.size > maxBytes) return "Larger than the " + bytes(maxBytes) + " upload limit";
      if (!file.size) return "The file is empty";
      return "";
    }

    function toast(message, bad) {
      if (!statusEl) return;
      statusEl.textContent = message;
      statusEl.className = "notice" + (bad ? " notice-warn" : "");
      statusEl.hidden = false;
    }

    function add(files) {
      Array.prototype.forEach.call(files, function (f) {
        var problem = rejectReason(f);
        if (!problem) {
          var dupe = queue.some(function (q) {
            return q.file.name === f.name && q.file.size === f.size;
          });
          if (dupe) problem = "is already in the list";
        }
        if (problem) { toast(f.name + ": " + problem, true); return; }
        queue.push({ file: f, state: "ready", pct: 0, message: "" });
      });
      paint();
    }

    function paint() {
      if (!listEl) return;
      clearChildren(listEl);
      queue.forEach(function (item, i) {
        var li = document.createElement("li");
        li.className = "fileitem" + (item.state === "done" ? " done"
                          : item.state === "error" ? " failed" : "");

        var name = document.createElement("span");
        name.className = "fname";
        name.textContent = item.file.name;

        var size = document.createElement("span");
        size.className = "fsize";
        size.textContent = bytes(item.file.size);

        var bar = document.createElement("span");
        bar.className = "fbar";
        var fill = document.createElement("span");
        fill.style.width = (item.state === "done" ? 100 : item.pct || 0) + "%";
        bar.appendChild(fill);

        li.appendChild(name);
        li.appendChild(size);
        li.appendChild(bar);

        if (item.message) {
          var res = document.createElement("span");
          res.className = "fresult";
          res.textContent = item.message;
          li.appendChild(res);
        }
        if (item.report_url) {
          var link = document.createElement("a");
          link.href = item.report_url;
          link.className = "fresult";
          link.textContent = "open report";
          li.appendChild(link);
        }
        if (item.state === "ready" || item.state === "error") {
          var rm = document.createElement("button");
          rm.type = "button";
          rm.className = "filedrop";
          rm.setAttribute("aria-label", "Remove " + item.file.name);
          rm.textContent = "\u00d7";
          rm.addEventListener("click", function () {
            queue.splice(i, 1);
            paint();
          });
          li.appendChild(rm);
        }
        listEl.appendChild(li);
      });

      var pending = queue.filter(function (q) {
        return q.state === "ready" || q.state === "error";
      });
      if (sendBtn) {
        sendBtn.disabled = !pending.length;
        sendBtn.textContent = pending.length
          ? "Upload " + pending.length + " file" + (pending.length > 1 ? "s" : "")
          : "Nothing queued";
      }
    }

    function describe(res, item) {
      if (res.status === "imported") {
        item.report_url = "/reports/" + res.report_id;
        return "Stored " + res.isolates + " isolate(s) and " + res.sensitivities +
          " susceptibility result(s)" + (res.target_hits ? " - " + targets(res) : "") +
          "." + (res.rows_skipped ? " (" + res.rows_skipped + " row(s) skipped)" : "");
      }
      if (res.status === "duplicate") {
        if (res.report_id) item.report_url = "/reports/" + res.report_id;
        return res.message || "This exact file is already stored.";
      }
      if (res.status === "empty") {
        return res.message || "No organism results could be read from this file.";
      }
      return res.message || "The file could not be imported.";
    }

    function targets(res) {
      var found = res.found || [];
      if (!found.length) return "no tracked target organism";
      var head = found.slice(0, 4).map(function (f) {
        return f.count + " " + f.name;
      }).join(", ");
      return head + (found.length > 4 ? " (+" + (found.length - 4) + " more)" : "");
    }

    function upload(item) {
      return new Promise(function (resolve) {
        var fd = new FormData();
        fd.append("file", item.file, item.file.name);
        if (dupBox && dupBox.checked) fd.append("allow_duplicate", "1");

        var xhr = new XMLHttpRequest();
        xhr.open("POST", zone.getAttribute("data-url") || "/api/upload", true);
        xhr.responseType = "json";
        xhr.upload.onprogress = function (e) {
          if (!e.lengthComputable) return;
          item.pct = Math.round(e.loaded * 100 / e.total);
          var pos = queue.indexOf(item);
          var bar = (listEl ? listEl.querySelectorAll(".fbar > span") : [])[pos];
          if (bar) bar.style.width = item.pct + "%";
        };
        function finish(state, message) {
          item.state = state;
          item.message = message;
          resolve();
        }
        xhr.onload = function () {
          var body = xhr.response || {};
          var res = (body.results && body.results[0]) || {};
          var good = xhr.status >= 200 && xhr.status < 300;
          var text;
          if (xhr.status === 413) {
            text = "Rejected: larger than the server's upload limit.";
          } else if (res.status) {
            text = describe(res, item);
          } else {
            text = body.error || "The server returned HTTP " + xhr.status + ".";
          }
          /* "imported" and "duplicate" both mean the data is safely in the
             database - a duplicate is a no-op, not a failure. */
          var stored = good && (res.status === "imported" || res.status === "duplicate");
          if (good && res.status === "imported") imported++;
          finish(stored ? "done" : "error", text);
        };
        xhr.onerror = function () {
          finish("error", "The request never reached the server.");
        };
        item.state = "sending";
        item.pct = 0;
        item.message = "";
        xhr.send(fd);
      });
    }

    function send() {
      var pending = queue.filter(function (q) {
        return q.state === "ready" || q.state === "error";
      });
      if (!pending.length) return;
      if (sendBtn) sendBtn.disabled = true;
      imported = 0;
      paint();
      /* Sequential on purpose: two big PDFs at once doubles peak memory on a
         modest lab server for no user-visible gain. */
      pending.reduce(function (chain, item) {
        return chain.then(function () { return upload(item).then(paint); });
      }, Promise.resolve()).then(function () {
        paint();
        if (imported) {
          toast(imported + " file(s) imported successfully.", false);
          window.setTimeout(function () { window.location.href = "/"; }, 1400);
        } else {
          toast("Nothing new was imported - see the reason beside each file.", true);
        }
      });
    }

    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) {
        e.preventDefault();
        zone.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) {
        e.preventDefault();
        zone.classList.remove("is-over");
      });
    });
    zone.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files) add(e.dataTransfer.files);
    });
    zone.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest("button, a, input, label")) return;
      if (picker) picker.click();
    });
    if (picker) {
      picker.addEventListener("change", function () {
        add(picker.files);
        picker.value = "";
      });
    }
    if (sendBtn) sendBtn.addEventListener("click", send);

    /* "Queue this sample" pulls a bundled example into the list, so the whole
       intake path can be exercised without hunting for a compatible file. */
    document.querySelectorAll("[data-queue-sample]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var url = btn.getAttribute("data-queue-sample");
        var name = btn.getAttribute("data-name") || url.split("/").pop();
        btn.disabled = true;
        fetch(url, { credentials: "same-origin" }).then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.blob();
        }).then(function (blob) {
          add([new File([blob], name, { type: blob.type || "application/octet-stream" })]);
          btn.textContent = "queued";
          window.setTimeout(function () {
            btn.textContent = "queue this sample";
            btn.disabled = false;
          }, 1800);
        }).catch(function () {
          btn.disabled = false;
          toast("Could not fetch " + name + " from the server.", true);
        });
      });
    });

    paint();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", followHash);
  } else {
    followHash();
  }
})();
