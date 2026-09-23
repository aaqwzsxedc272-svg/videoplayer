using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text.RegularExpressions;
using TMPro;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.Video;

public static class VRUnityRuntimePatch
{
    private sealed class State
    {
        public TMP_Text SubtitleText;
        public VideoPlayer CachedPlayer;          // FIX: cache instead of reflecting every frame
        public readonly List<SubtitleCue> Cues = new List<SubtitleCue>();
        public int CueIndex = -1;
        public bool Visible = true;
        public bool SWasDown;
    }

    private struct SubtitleCue
    {
        public double Start;
        public double End;
        public string Text;
    }

    private static readonly Dictionary<int, State> States = new Dictionary<int, State>();

    // FIX: cache FieldInfo per controller type so reflection only runs once per type
    private static readonly Dictionary<Type, FieldInfo> FieldCache = new Dictionary<Type, FieldInfo>();

    // FIX: cache canvas lookup so FindObjectOfType never runs on hot paths
    private static Canvas _cachedCanvas;

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    public static void OnStart(MonoBehaviour controller)
    {
        if (controller == null)
            return;

        ApplyBlackFallback(controller);

        State state = GetState(controller);

        // Cache the VideoPlayer once at startup
        state.CachedPlayer = GetVideoPlayer(controller);

        SetupSubtitleUI(state);
        LoadSubtitleFile(state, FindSubtitleArgument(Environment.GetCommandLineArgs()));

        if (state.CachedPlayer != null)
        {
            ConfigureVideoPlayerForHighBitrate(state.CachedPlayer);
            state.CachedPlayer.errorReceived += (vp, msg) => ApplyBlackFallback(controller);
        }
    }

    public static void OnUpdate(MonoBehaviour controller)
    {
        if (controller == null)
            return;

        State state = GetState(controller);

        // FIX: use cached player — no reflection, no dictionary lookup per frame
        VideoPlayer player = state.CachedPlayer;
        if (player == null || state.SubtitleText == null)
            return;

        // Edge-detection for S key toggle
        bool sDown = Input.GetKey(KeyCode.S);
        if (sDown && !state.SWasDown)
            state.Visible = !state.Visible;
        state.SWasDown = sDown;

        if (!state.Visible || state.Cues.Count == 0)
        {
            state.SubtitleText.text = "";
            return;
        }

        double now = player.time;

        // Fast path: current cue is still active
        if (state.CueIndex >= 0 && state.CueIndex < state.Cues.Count)
        {
            SubtitleCue current = state.Cues[state.CueIndex];
            if (now >= current.Start && now <= current.End)
            {
                // text already set; nothing to do
                return;
            }
        }

        // FIX: binary search instead of linear scan — O(log n) vs O(n)
        int idx = BinarySearchCue(state.Cues, now);
        if (idx >= 0)
        {
            state.CueIndex = idx;
            state.SubtitleText.text = state.Cues[idx].Text;
        }
        else
        {
            state.CueIndex = -1;
            state.SubtitleText.text = "";
        }
    }

    // -------------------------------------------------------------------------
    // VideoPlayer tuning for high-bitrate 4K/60fps VR content
    // -------------------------------------------------------------------------

    private static void ConfigureVideoPlayerForHighBitrate(VideoPlayer vp)
    {
        // DROP frames when the decoder falls behind instead of stalling the
        // whole player. This is the single most important setting for 4K/60fps:
        // without it, one slow decode causes a cascade of stalls.
        vp.skipOnDrop = true;

        // DSP time ties the video clock to the audio DSP clock, which is rock-
        // steady and independent of Unity's frame rate. GameTime (the default)
        // fights the video clock at 60fps because Unity's Update() loop doesn't
        // run at a perfectly fixed interval, causing constant micro-resyncs.
        vp.timeUpdateMode = VideoTimeUpdateMode.DSPTime;

        // Don't block the render thread waiting for the first frame to decode.
        // Unity stalls the entire main thread until the first frame is ready by
        // default, which causes a visible freeze on load for large files.
        vp.waitForFirstFrame = false;

        // Route audio directly to the DSP output, bypassing Unity's AudioSource
        // mixer. This removes a layer of buffering that adds jitter and latency
        // at 60fps. The mixer path is fine for short clips; for a 44-minute 4K
        // VR video it is a constant source of audio/video desync.
        vp.audioOutputMode = VideoAudioOutputMode.Direct;

        // 0.35 s tolerance before forcing a clock resync. The default (~10-50 ms)
        // is far too tight for 4K software decode on this hardware. We raise it
        // to 0.35 s rather than 0.2 s because this system uses Bluetooth
        // headphones (A2DP), which have ~150-200 ms of inherent audio latency.
        // With 0.2 the player resyncs constantly because Bluetooth delivery
        // jitter looks like clock drift. 0.35 absorbs that without being
        // perceptible to the viewer.
        vp.clockTolerance = 0.35;
    }

    // -------------------------------------------------------------------------
    // Binary search for the cue active at 'now'
    // -------------------------------------------------------------------------

    private static int BinarySearchCue(List<SubtitleCue> cues, double now)
    {
        int lo = 0, hi = cues.Count - 1;
        while (lo <= hi)
        {
            int mid = (lo + hi) >> 1;
            SubtitleCue c = cues[mid];
            if (now < c.Start)
                hi = mid - 1;
            else if (now > c.End)
                lo = mid + 1;
            else
                return mid;   // now is within [start, end]
        }
        return -1;
    }

    // -------------------------------------------------------------------------
    // State management
    // -------------------------------------------------------------------------

    private static State GetState(MonoBehaviour controller)
    {
        int id = controller.GetInstanceID();
        State state;
        if (!States.TryGetValue(id, out state))
        {
            state = new State();
            States[id] = state;
        }
        return state;
    }

    // FIX: cache FieldInfo per Type so GetField() runs at most once per controller class
    private static VideoPlayer GetVideoPlayer(MonoBehaviour controller)
    {
        Type t = controller.GetType();
        FieldInfo field;
        if (!FieldCache.TryGetValue(t, out field))
        {
            field = t.GetField("videoPlayer",
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            FieldCache[t] = field;   // null is a valid cached value (means "not found")
        }
        return field != null ? field.GetValue(controller) as VideoPlayer : null;
    }

    // -------------------------------------------------------------------------
    // Black fallback for video errors
    // -------------------------------------------------------------------------

    private static void ApplyBlackFallback(MonoBehaviour controller)
    {
        Camera cam = Camera.main;
        if (cam != null)
        {
            cam.clearFlags = CameraClearFlags.SolidColor;
            cam.backgroundColor = Color.black;
        }
        RenderSettings.skybox = null;

        MeshRenderer rend = controller.GetComponentInChildren<MeshRenderer>();
        if (rend != null && rend.material != null)
            rend.material.mainTexture = Texture2D.blackTexture;
    }

    // -------------------------------------------------------------------------
    // Subtitle UI setup
    // -------------------------------------------------------------------------

    private static void SetupSubtitleUI(State state)
    {
        if (state.SubtitleText != null)
            return;

        // FIX: use cached canvas so FindObjectOfType never runs after the first call
        if (_cachedCanvas == null)
            _cachedCanvas = UnityEngine.Object.FindObjectOfType<Canvas>();

        if (_cachedCanvas == null)
        {
            GameObject canvasObj = new GameObject("SubtitleCanvas");
            _cachedCanvas = canvasObj.AddComponent<Canvas>();
            _cachedCanvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvasObj.AddComponent<CanvasScaler>();
            canvasObj.AddComponent<GraphicRaycaster>();
        }

        GameObject subObj = new GameObject("VRSubtitleText");
        subObj.transform.SetParent(_cachedCanvas.transform, false);

        TextMeshProUGUI text = subObj.AddComponent<TextMeshProUGUI>();
        text.text = "";
        text.alignment = TextAlignmentOptions.Center;
        text.enableWordWrapping = true;
        text.fontSize = 42f;
        text.color = Color.white;          // FIX: changed from yellow to white
        text.outlineColor = Color.black;
        text.outlineWidth = 0.25f;
        text.raycastTarget = false;

        RectTransform rt = subObj.GetComponent<RectTransform>();
        rt.anchorMin = new Vector2(0.05f, 0f);
        rt.anchorMax = new Vector2(0.95f, 0f);
        rt.pivot = new Vector2(0.5f, 0f);
        rt.anchoredPosition = new Vector2(0f, 115f);
        rt.sizeDelta = new Vector2(0f, 180f);

        state.SubtitleText = text;
    }

    // -------------------------------------------------------------------------
    // Subtitle argument parsing
    // -------------------------------------------------------------------------

    private static string FindSubtitleArgument(string[] args)
    {
        for (int i = 2; i < args.Length; i++)
        {
            string arg = args[i] ?? "";
            if (arg.StartsWith("--subtitle=", StringComparison.OrdinalIgnoreCase))
                return arg.Substring("--subtitle=".Length).Trim('"');
            if (arg.StartsWith("--sub=", StringComparison.OrdinalIgnoreCase))
                return arg.Substring("--sub=".Length).Trim('"');
            if (IsSubtitlePath(arg))
                return arg;
        }
        return "";
    }

    private static bool IsSubtitlePath(string path)
    {
        return path.EndsWith(".srt", StringComparison.OrdinalIgnoreCase)
            || path.EndsWith(".vtt", StringComparison.OrdinalIgnoreCase)
            || path.EndsWith(".ass", StringComparison.OrdinalIgnoreCase)
            || path.EndsWith(".ssa", StringComparison.OrdinalIgnoreCase);
    }

    // -------------------------------------------------------------------------
    // Subtitle file loading & parsing
    // -------------------------------------------------------------------------

    private static void LoadSubtitleFile(State state, string path)
    {
        if (string.IsNullOrEmpty(path) || !File.Exists(path))
            return;

        try
        {
            // FIX: explicit UTF-8 with BOM detection to handle common encodings correctly
            ParseSubtitleText(state, File.ReadAllText(path, System.Text.Encoding.UTF8));
        }
        catch
        {
            state.Cues.Clear();
        }
    }

    private static void ParseSubtitleText(State state, string raw)
    {
        state.Cues.Clear();
        state.CueIndex = -1;

        raw = (raw ?? "").Replace("\r\n", "\n").Replace('\r', '\n');
        string[] blocks = Regex.Split(raw.Trim(), "\n{2,}");
        foreach (string block in blocks)
        {
            string[] lines = block.Split('\n');
            int timeLine = -1;
            for (int i = 0; i < lines.Length; i++)
            {
                if (lines[i].Contains("-->"))
                {
                    timeLine = i;
                    break;
                }
            }
            if (timeLine < 0)
                continue;

            string[] parts = lines[timeLine].Split(new[] { "-->" }, StringSplitOptions.None);
            if (parts.Length < 2)
                continue;

            double start = ParseSubtitleTime(parts[0]);
            double end   = ParseSubtitleTime(parts[1]);
            if (end <= start)
                continue;

            List<string> body = new List<string>();
            for (int i = timeLine + 1; i < lines.Length; i++)
            {
                string line = CleanSubtitleLine(lines[i]);
                if (!string.IsNullOrWhiteSpace(line))
                    body.Add(line);
            }
            if (body.Count > 0)
                state.Cues.Add(new SubtitleCue
                {
                    Start = start,
                    End   = end,
                    Text  = string.Join("\n", body.ToArray())
                });
        }

        if (state.Cues.Count == 0)
            ParseAssDialogueLines(state, raw);
    }

    private static void ParseAssDialogueLines(State state, string raw)
    {
        foreach (string rawLine in (raw ?? "").Split('\n'))
        {
            string line = rawLine.Trim();
            if (!line.StartsWith("Dialogue:", StringComparison.OrdinalIgnoreCase))
                continue;

            string payload = line.Substring(line.IndexOf(':') + 1).Trim();
            string[] parts = payload.Split(new[] { ',' }, 10);
            if (parts.Length < 10)
                continue;

            double start = ParseSubtitleTime(parts[1]);
            double end   = ParseSubtitleTime(parts[2]);
            if (end <= start)
                continue;

            string text = parts[9].Replace("\\N", "\n").Replace("\\n", "\n").Replace("\\h", " ");
            text = Regex.Replace(text, @"\{\\.*?\}", "");
            text = CleanSubtitleLine(text);
            if (!string.IsNullOrWhiteSpace(text))
                state.Cues.Add(new SubtitleCue { Start = start, End = end, Text = text });
        }
    }

    private static double ParseSubtitleTime(string value)
    {
        Match m = Regex.Match(value ?? "", @"(?:(\d+):)?(\d{1,2}):(\d{2})[\.,](\d{1,3})");
        if (!m.Success)
            return 0;

        double hours   = string.IsNullOrEmpty(m.Groups[1].Value) ? 0 : double.Parse(m.Groups[1].Value);
        double minutes = double.Parse(m.Groups[2].Value);
        double seconds = double.Parse(m.Groups[3].Value);
        string msText  = m.Groups[4].Value.PadRight(3, '0').Substring(0, 3);
        double millis  = double.Parse(msText);
        return hours * 3600 + minutes * 60 + seconds + millis / 1000.0;
    }

    private static string CleanSubtitleLine(string line)
    {
        line = Regex.Replace(line ?? "", @"<[^>]+>", "");
        return line.Replace("&nbsp;", " ")
                   .Replace("&amp;",  "&")
                   .Replace("&lt;",   "<")
                   .Replace("&gt;",   ">")
                   .Trim();
    }
}
