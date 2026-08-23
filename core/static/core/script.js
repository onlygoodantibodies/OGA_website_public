document.addEventListener('DOMContentLoaded', function () {

    // ── HAMBURGER MENU ──
    // Guard against double-load: script.js is included twice in some templates.
    // Use a data attribute on the hamburger to prevent registering toggle twice.
    var menu = document.querySelector('nav ul');
    var hamburger = document.querySelector('.hamburger');

    if (hamburger && menu && !hamburger.dataset.bound) {
        hamburger.dataset.bound = 'true';

        hamburger.addEventListener('click', function () {
            menu.classList.toggle('show');
        });

        document.addEventListener('click', function (event) {
            if (!menu.contains(event.target) && !hamburger.contains(event.target)) {
                menu.classList.remove('show');
            }
        });

        document.querySelectorAll('nav ul li a').forEach(function (link) {
            link.addEventListener('click', function () {
                menu.classList.remove('show');
            });
        });
    }

    // ── COOKIE BANNER ──
    var cookieBanner = document.getElementById('cookie-banner');
    var acceptButton = document.getElementById('accept-cookies');
    var rejectButton = document.getElementById('reject-cookies');

    if (cookieBanner) {
        var cookiesAccepted = document.cookie.indexOf('cookies_accepted=true') !== -1;
        var cookiesRejected = document.cookie.indexOf('cookies_accepted=false') !== -1;

        if (cookiesAccepted || cookiesRejected) {
            cookieBanner.style.display = 'none';
        }

        if (cookiesAccepted) {
            loadGoogleAnalytics();
        }

        if (acceptButton) {
            acceptButton.addEventListener('click', function () {
                document.cookie = 'cookies_accepted=true; max-age=' + (60 * 60 * 24 * 30) + '; path=/';
                cookieBanner.style.display = 'none';
                loadGoogleAnalytics();
            });
        }

        if (rejectButton) {
            rejectButton.addEventListener('click', function () {
                document.cookie = 'cookies_accepted=false; max-age=' + (60 * 60 * 24 * 30) + '; path=/';
                cookieBanner.style.display = 'none';
            });
        }
    }

    // ── COPY CITATION ──
    var copyButton = document.getElementById('copy-btn');
    var citationText = document.getElementById('citation-text');

    if (copyButton && citationText) {
        copyButton.addEventListener('click', function () {
            if (citationText.innerText.trim() === '') {
                return;
            }
            navigator.clipboard.writeText(citationText.textContent.trim())
                .then(function () {
                    alert('Citation copied to clipboard!');
                })
                .catch(function (err) {
                    console.error('Failed to copy:', err);
                });
        });
    }

});

// ── GOOGLE ANALYTICS ──
function loadGoogleAnalytics() {
    if (window._gaLoaded) return;
    window._gaLoaded = true;

    var scriptTag = document.createElement('script');
    scriptTag.async = true;
    scriptTag.src = 'https://www.googletagmanager.com/gtag/js?id=G-10W2PEF9SW';
    document.head.appendChild(scriptTag);

    scriptTag.onload = function () {
        window.dataLayer = window.dataLayer || [];
        function gtag() { dataLayer.push(arguments); }
        gtag('js', new Date());
        gtag('config', 'G-10W2PEF9SW', { 'anonymize_ip': true });
    };
}

// ── COPY CURRENT URL ──
function copyCurrentURL() {
    var url = window.location.href;
    navigator.clipboard.writeText(url).then(function () {
        var confirmation = document.getElementById('copy-confirmation');
        if (confirmation) {
            confirmation.style.display = 'block';
            setTimeout(function () {
                confirmation.style.display = 'none';
            }, 2500);
        }
    });
}
