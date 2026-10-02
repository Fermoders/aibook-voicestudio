using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("AIBook VoiceStudio")]
[assembly: AssemblyDescription("Standalone local audiobook narration")]
[assembly: AssemblyVersion("1.2.0.0")]

internal static class VoiceStudioLauncher
{
    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr window, int command);

    private static bool ActivateExisting(string root)
    {
        string executable = Path.Combine(root, "runtime", "python", "pythonw.exe");
        foreach (Process candidate in Process.GetProcessesByName("pythonw"))
        {
            using (candidate)
            {
                try
                {
                    if (!candidate.HasExited && candidate.MainWindowTitle == "AIBook VoiceStudio" &&
                        String.Equals(candidate.MainModule.FileName, executable, StringComparison.OrdinalIgnoreCase))
                    {
                        ShowWindow(candidate.MainWindowHandle, 9);
                        SetForegroundWindow(candidate.MainWindowHandle);
                        return true;
                    }
                }
                catch (System.ComponentModel.Win32Exception) { }
                catch (InvalidOperationException) { }
            }
        }
        return false;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        if (ActivateExisting(root)) return 0;
        string identity;
        using (SHA256 hash = SHA256.Create())
        {
            identity = BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(root.ToLowerInvariant()))).Replace("-", "");
        }
        using (Mutex mutex = new Mutex(false, "Local\\AIBookVoiceStudio_" + identity))
        {
            bool acquired;
            try { acquired = mutex.WaitOne(0); }
            catch (AbandonedMutexException) { acquired = true; }
            if (!acquired)
            {
                ActivateExisting(root);
                return 0;
            }
            try
            {
                string runtime = Path.Combine(root, "runtime", "python");
                string python = Path.Combine(runtime, "pythonw.exe");
                string app = Path.Combine(root, "app");
                string entry = Path.Combine(app, "voice_studio_main.py");
                if (!File.Exists(python) || !File.Exists(entry))
                    throw new FileNotFoundException("The portable folder is incomplete. Keep the EXE with app, runtime and data.");
                ProcessStartInfo start = new ProcessStartInfo(python);
                StringBuilder arguments = new StringBuilder(Quote(entry));
                foreach (string arg in args) arguments.Append(" ").Append(Quote(arg));
                start.Arguments = arguments.ToString();
                start.WorkingDirectory = app;
                start.UseShellExecute = false;
                start.CreateNoWindow = true;
                start.WindowStyle = ProcessWindowStyle.Hidden;
                start.EnvironmentVariables["PYTHONHOME"] = runtime;
                start.EnvironmentVariables["PYTHONPATH"] = app;
                start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
                start.EnvironmentVariables["PYTHONUTF8"] = "1";
                start.EnvironmentVariables["AIBOOK_ROOT"] = root;
                start.EnvironmentVariables["AIBOOK_DATA_DIR"] = Path.Combine(root, "data", "voicestudio");
                start.EnvironmentVariables["AIBOOK_OUTPUT_DIR"] = Path.Combine(root, "outputs");
                start.EnvironmentVariables["AIBOOK_VOICESTUDIO_MODEL_DIR"] = Path.Combine(root, "data", "models", "OmniVoice");
                start.EnvironmentVariables["HF_HOME"] = Path.Combine(root, "data", "huggingface");
                start.EnvironmentVariables["HF_HUB_OFFLINE"] = "1";
                start.EnvironmentVariables["TRANSFORMERS_OFFLINE"] = "1";
                start.EnvironmentVariables["HF_HUB_DISABLE_TELEMETRY"] = "1";
                start.EnvironmentVariables["PATH"] = Path.Combine(root, "runtime", "ffmpeg") + ";" + runtime + ";" + Path.Combine(runtime, "DLLs") + ";" + start.EnvironmentVariables["PATH"];
                using (Process process = Process.Start(start))
                {
                    process.WaitForExit();
                    return process.ExitCode;
                }
            }
            catch (Exception error)
            {
                MessageBox.Show(error.Message, "AIBook VoiceStudio", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
            finally { mutex.ReleaseMutex(); }
        }
    }

    private static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { backslashes++; continue; }
            if (character == '"')
            {
                result.Append('\\', backslashes * 2 + 1).Append('"');
            }
            else
            {
                result.Append('\\', backslashes).Append(character);
            }
            backslashes = 0;
        }
        return result.Append('\\', backslashes * 2).Append('"').ToString();
    }
}
