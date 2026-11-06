// docs/js/doc_behavior.js

document.addEventListener("DOMContentLoaded", () => {
    buildOnThisPageMenu();
    setupScrollSpy();
    setupActiveSidebarLink();
});

/**
 * Dynamically builds the "On this page" TOC menu by parsing body h2 and h3 elements.
 */
function buildOnThisPageMenu() {
    const container = document.querySelector(".on-this-page-menu");
    if (!container) return;

    const headings = document.querySelectorAll(".content-body h2, .content-body h3");
    if (headings.length === 0) {
        document.querySelector(".on-this-page").style.display = "none";
        return;
    }

    const ul = document.createElement("ul");
    ul.style.listStyle = "none";

    headings.forEach((heading) => {
        // Generate a valid ID if not explicitly declared in the HTML template
        if (!heading.id) {
            heading.id = heading.textContent
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, "-")
                .replace(/(^-|-$)/g, "");
        }

        const li = document.createElement("li");
        li.style.marginBottom = "8px";
        li.style.paddingLeft = heading.tagName === "H3" ? "12px" : "0px";

        const a = document.createElement("a");
        a.href = `#${heading.id}`;
        a.textContent = heading.textContent;
        a.style.fontSize = heading.tagName === "H3" ? "12px" : "13px";
        a.style.color = "var(--text-muted)";
        a.style.transition = "color 0.2s ease";
        a.className = "toc-link";
        a.setAttribute("data-target", heading.id);

        li.appendChild(a);
        ul.appendChild(li);
    });

    container.appendChild(ul);
}

/**
 * Updates the highlighted active link in the right-side Table of Contents as the user scrolls.
 */
function setupScrollSpy() {
    const headings = document.querySelectorAll(".content-body h2, .content-body h3");
    const tocLinks = document.querySelectorAll(".toc-link");

    window.addEventListener("scroll", () => {
        let activeId = "";
        const scrollPosition = window.scrollY + 120; // Anchor buffer offset

        headings.forEach((heading) => {
            if (heading.offsetTop <= scrollPosition) {
                activeId = heading.id;
            }
        });

        tocLinks.forEach((link) => {
            if (link.getAttribute("data-target") === activeId) {
                link.style.color = "var(--accent-color)";
                link.style.fontWeight = "bold";
            } else {
                link.style.color = "var(--text-muted)";
                link.style.fontWeight = "normal";
            }
        });
    });
}

/**
 * Evaluates the active HTML filename and applies active highlights to the Left Sidebar links.
 */
function setupActiveSidebarLink() {
    const currentPath = window.location.pathname;
    const filename = currentPath.substring(currentPath.lastIndexOf("/") + 1);
    const sidebarLinks = document.querySelectorAll(".sidebar a");

    sidebarLinks.forEach((link) => {
        const href = link.getAttribute("href");
        if (href === filename || (filename === "" && href === "index.html")) {
            link.style.color = "var(--accent-color)";
            link.style.fontWeight = "bold";
        }
    });
}