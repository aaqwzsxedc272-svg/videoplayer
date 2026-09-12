const { chromium } = require('playwright');
const { exec, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

function findInstalledBrave() {
    function usable(candidate) {
        if (!candidate) return '';
        const expanded = String(candidate).replace(/^"|"$/g, '').trim();
        if (!expanded || expanded.toLowerCase().includes('brave-automation-tool')) return '';
        try {
            const stat = fs.statSync(expanded);
            if (stat.isFile()) return expanded;
            if (stat.isDirectory()) {
                const exe = process.platform === 'win32' ? 'brave.exe' : 'brave';
                const nested = path.join(expanded, exe);
                if (fs.existsSync(nested)) return nested;
            }
        } catch {}
        return '';
    }

    for (const key of ['VIDEO_PLAYER_BRAVE_PATH', 'BRAVE_PATH', 'BRAVE_EXE']) {
        const found = usable(process.env[key]);
        if (found) return found;
    }

    if (process.platform === 'win32') {
        const roots = [process.env.LOCALAPPDATA, process.env.ProgramFiles, process.env['ProgramFiles(x86)']].filter(Boolean);
        const rels = [
            ['BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'],
            ['BraveSoftware', 'Brave-Browser-Beta', 'Application', 'brave.exe'],
            ['BraveSoftware', 'Brave-Browser-Nightly', 'Application', 'brave.exe'],
            ['BraveSoftware', 'Brave-Browser-Dev', 'Application', 'brave.exe'],
        ];
        for (const root of roots) {
            for (const rel of rels) {
                const found = usable(path.join(root, ...rel));
                if (found) return found;
            }
        }
        try {
            const output = execSync('where brave.exe', { encoding: 'utf8', windowsHide: true });
            for (const line of output.split(/\r?\n/)) {
                const found = usable(line);
                if (found) return found;
            }
        } catch {}
    } else {
        for (const cmd of ['brave-browser', 'brave']) {
            try {
                const output = execSync(`command -v ${cmd}`, { encoding: 'utf8' });
                const found = usable(output.split(/\r?\n/)[0]);
                if (found) return found;
            } catch {}
        }
    }
    return '';
}

function selectPreferredPlaylistUrl(urls) {
    const items = Array.from(
        new Set((urls || []).filter(url => typeof url === 'string' && url.trim()))
    );

    return (
        items.find(url => /playlist\.m3u8(?:[?#]|$)/i.test(url)) ||
        items.find(url => /\.m3u8(?:[?#]|$)/i.test(url)) ||
        ''
    );
}

(async () => {
    const targetUrl = process.argv[2];

    if (!targetUrl) {
        console.log('Usage: node capture.js "<url>"');
        process.exit(1);
    }

    const bravePath = findInstalledBrave();
    if (!bravePath) {
        console.error('Installed Brave browser was not found.');
        process.exit(1);
    }

    const browser = await chromium.launch({
        executablePath: bravePath,
        headless: false,
        args: [
            '--disable-blink-features=AutomationControlled',
            '--window-position=-32000,-32000',
            '--window-size=800,600'
        ]
    });

    const context = await browser.newContext({
        viewport: {
            width: 800,
            height: 600
        },
        userAgent:
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    });

    const page = await context.newPage();

    // Try to minimize all Chrome/Chromium windows after launch
    setTimeout(() => {
        exec(`
powershell -WindowStyle Hidden -Command "
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class Win {
    [DllImport(\\"user32.dll\\")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}
'@;
Get-Process chrome,brave -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.MainWindowHandle -ne 0) {
        [Win]::ShowWindowAsync($_.MainWindowHandle, 6)
    }
}
"
        `, () => {});
    }, 1500);

    const surritUrls = new Set();

    let lastFound = Date.now();
    let title = '';

    page.on('request', request => {
        try {
            const url = request.url();

            if (
                url.includes('surrit.com') &&
                url.includes('.m3u8')
            ) {
                if (!surritUrls.has(url)) {
                    surritUrls.add(url);
                    lastFound = Date.now();

                    console.log('\nFOUND SURRIT URL:');
                    console.log(url);
                }
            }
        } catch {}
    });

    try {
        await page.goto(targetUrl, {
            waitUntil: 'domcontentloaded',
            timeout: 60000
        });

        await page.waitForTimeout(3000);

        const titleCandidates = [];

        try {
            const h1 = page.locator('h1').first();

            if (await h1.count()) {
                const txt = await h1.textContent();

                if (txt && txt.trim()) {
                    titleCandidates.push(txt.trim());
                }
            }
        } catch {}

        try {
            const ogTitle = await page
                .locator('meta[property="og:title"]')
                .getAttribute('content');

            if (ogTitle && ogTitle.trim()) {
                titleCandidates.push(ogTitle.trim());
            }
        } catch {}

        try {
            const docTitle = await page.title();

            if (docTitle && docTitle.trim()) {
                titleCandidates.push(docTitle.trim());
            }
        } catch {}

        title =
            titleCandidates
                .filter(Boolean)
                .sort((a, b) => b.length - a.length)[0] ||
            'Unknown Title';

        console.log('\n========================================');
        console.log('TITLE:', title);
        console.log('URL  :', targetUrl);
        console.log('========================================');
        console.log('\nWaiting for Surrit URLs...\n');

    } catch (err) {
        console.error('Navigation failed:', err.message);

        await browser.close();
        process.exit(1);
    }

    const interval = setInterval(async () => {
        const idleSeconds = (Date.now() - lastFound) / 1000;

        // Auto-close after 3 seconds without new Surrit URLs
        if (idleSeconds >= 3) {
            clearInterval(interval);

            const discoveredUrls = [...surritUrls];
            const playlistUrl = selectPreferredPlaylistUrl(discoveredUrls);

            console.log('\n========================================');
            console.log('FINAL RESULTS');
            console.log('========================================');
            console.log('TITLE:', title);
            console.log('SOURCE_URL:', targetUrl);

            if (playlistUrl) {
                console.log('PLAYLIST_M3U8:', playlistUrl);
            }

            if (discoveredUrls.length === 0) {
                console.log('\nNo Surrit links found.');
            } else {
                console.log('\nSURRIT LINKS:\n');

                for (const url of discoveredUrls) {
                    console.log(url);
                }
            }

            console.log(
                'RESULT_JSON:',
                JSON.stringify({
                    title,
                    sourceUrl: targetUrl,
                    playlistUrl,
                    urls: discoveredUrls
                })
            );

            console.log('\nClosing browser...');

            await browser.close();
            process.exit(0);
        }
    }, 500);

})();
