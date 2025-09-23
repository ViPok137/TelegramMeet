using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace TelegramMeet
{
    public class IniFile
    {
        private readonly string path;

        public IniFile(string iniPath)
        {
            path = iniPath;

            string dir = Path.GetDirectoryName(path);
            if (!Directory.Exists(dir))
                Directory.CreateDirectory(dir);

            if (!File.Exists(path))
            {
                using (StreamWriter sw = new StreamWriter(path, false, Encoding.UTF8))
                {
                    sw.WriteLine("[Settings]");
                    sw.WriteLine("Language=Russian"); // язык по умолчанию
                    sw.WriteLine("PythonIns=False");
                }
            }
        }

        [DllImport("kernel32", CharSet = CharSet.Unicode)]
        private static extern long WritePrivateProfileString(string section, string key, string val, string filePath);

        [DllImport("kernel32", CharSet = CharSet.Unicode)]
        private static extern int GetPrivateProfileString(string section, string key, string def, StringBuilder retVal, int size, string filePath);

        public void Write(string section, string key, string value)
        {
            WritePrivateProfileString(section, key, value, path);
        }

        public string Read(string section, string key)
        {
            var retVal = new StringBuilder(1024);
            GetPrivateProfileString(section, key, "", retVal, retVal.Capacity, path);
            return retVal.ToString();
        }
    }
}
