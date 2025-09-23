using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Xml.Linq;

namespace TelegramMeet
{
    public partial class Form1 : Form
    {
        private XDocument languageDoc;
        private IniFile ini;
        private string baseDir;

        public Form1()
        {
            InitializeComponent();

            baseDir = AppDomain.CurrentDomain.BaseDirectory;

            // Подключаем INI
            string iniPath = Path.Combine(baseDir, "settings.ini");
            ini = new IniFile(iniPath);

            // Проверка Python через INI
            CheckPythonFlag();

            // Загружаем языки
            InitializeLanguages();

            // Восстанавливаем язык из INI
            string savedLang = ini.Read("Settings", "Language");
            if (string.IsNullOrEmpty(savedLang)) savedLang = "Russian";

            if (comboBoxLanguage.Items.Contains(savedLang))
                comboBoxLanguage.SelectedItem = savedLang;
            else if (comboBoxLanguage.Items.Count > 0)
                comboBoxLanguage.SelectedIndex = 0;

            LoadLanguage(comboBoxLanguage.SelectedItem?.ToString() ?? "Russian");
            ApplyTranslation();

            comboBoxLanguage.SelectedIndexChanged += ComboBoxLanguage_SelectedIndexChanged;

            labelApi.Text = Translate("labelAPI");

            // Подставляем токен из INI в TextBox. Используем string.IsNullOrWhiteSpace
            // для более надежной проверки наличия токена.
            string savedToken = ini.Read("Settings", "TelegramToken");
            if (!string.IsNullOrWhiteSpace(savedToken))
                botAPI.Text = savedToken;
            else
                botAPI.Text = Translate("TextBox");

            // Добавляем обработчик для события "Click" на botAPI.Text
            botAPI.Click += botAPI_Click;
        }

        private void CheckPythonFlag()
        {
            string flag = ini.Read("Settings", "PythonIns");
            if (string.IsNullOrEmpty(flag)) flag = "False";

            if (flag.Equals("False", StringComparison.OrdinalIgnoreCase))
            {
                try
                {
                    string batPath = Path.Combine(baseDir, "pyins.bat");

                    if (File.Exists(batPath))
                    {
                        var process = new Process
                        {
                            StartInfo = new ProcessStartInfo
                            {
                                FileName = batPath,
                                UseShellExecute = true
                            }
                        };
                        process.Start();
                        process.WaitForExit();

                        ini.Write("Settings", "PythonIns", "True");
                    }
                    else
                    {
                        MessageBox.Show(Translate("BatNotFoundError"),
                                        Translate("ErrorTitle"),
                                        MessageBoxButtons.OK,
                                        MessageBoxIcon.Error);
                    }
                }
                catch (Exception ex)
                {
                    MessageBox.Show($"{Translate("BatRunError")}: {ex.Message}",
                                    Translate("ErrorTitle"),
                                    MessageBoxButtons.OK,
                                    MessageBoxIcon.Error);
                }
            }
        }

        private void InitializeLanguages()
        {
            string langFolder = Path.Combine(baseDir, "Language");
            if (!Directory.Exists(langFolder)) return;

            var files = Directory.GetFiles(langFolder, "*.xml");
            comboBoxLanguage.Items.Clear();

            foreach (var file in files)
                comboBoxLanguage.Items.Add(Path.GetFileNameWithoutExtension(file));
        }

        private void ComboBoxLanguage_SelectedIndexChanged(object sender, EventArgs e)
        {
            if (comboBoxLanguage.SelectedItem != null)
            {
                string selectedLang = comboBoxLanguage.SelectedItem.ToString();
                ini.Write("Settings", "Language", selectedLang);
                LoadLanguage(selectedLang);
                ApplyTranslation();
            }
        }

        private void LoadLanguage(string lang)
        {
            string langPath = Path.Combine(baseDir, "Language", lang + ".xml");
            if (File.Exists(langPath))
                languageDoc = XDocument.Load(langPath);
            else
            {
                languageDoc = null;
                MessageBox.Show($"Языковой файл '{lang}.xml' не найден. Будет использован ключ как текст.", "Ошибка", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }

        private string Translate(string key)
        {
            if (languageDoc == null) return key;
            var element = languageDoc.Root.Element(key);
            if (element == null)
            {
                Debug.WriteLine($"Перевод для '{key}' не найден.");
                return key;
            }
            return element.Value;
        }

        private void ApplyTranslation()
        {
            this.Text = Translate("FormTitle");
            button1.Text = Translate("buttonText");
            linkLabel1.Text = Translate("HelpLink");
        }

        private async void button1_Click(object sender, EventArgs e)
        {
            string token = botAPI.Text.Trim();

            if (string.IsNullOrEmpty(token) || token == Translate("TextBox"))
            {
                MessageBox.Show(Translate("EmptyTokenError"), Translate("ErrorTitle"), MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            if (!Regex.IsMatch(token, @"^\d+:[\w-]+$"))
            {
                MessageBox.Show(Translate("InvalidFormatError"), Translate("ErrorTitle"), MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            if (await CheckTelegramToken(token))
            {
                // Сохраняем токен в INI
                ini.Write("Settings", "TelegramToken", token);

                string filePath = Path.Combine(baseDir, "TelegramBotMeetCore.py");
                if (File.Exists(filePath))
                {
                    Process.Start(new ProcessStartInfo
                    {
                        FileName = "python",
                        Arguments = $"\"{filePath}\"",
                        UseShellExecute = false
                    });
                }
                else
                {
                    MessageBox.Show(Translate("PyNotFoundError"),
                                    Translate("ErrorTitle"),
                                    MessageBoxButtons.OK,
                                    MessageBoxIcon.Error);
                }
            }
            else
            {
                MessageBox.Show(Translate("InvalidTokenError"), Translate("ErrorTitle"), MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private async Task<bool> CheckTelegramToken(string token)
        {
            try
            {
                using (HttpClient client = new HttpClient())
                {
                    string url = $"https://api.telegram.org/bot{token}/getMe";
                    HttpResponseMessage response = await client.GetAsync(url);
                    return response.IsSuccessStatusCode;
                }
            }
            catch { return false; }
        }

        private void linkLabel1_LinkClicked(object sender, LinkLabelLinkClickedEventArgs e)
        {
            Process.Start("https://core.telegram.org/api");
        }

        private void botAPI_TextChanged(object sender, EventArgs e)
        {

        }

        private void botAPI_Click(object sender, EventArgs e)
        {
            // Убираем текст-подсказку, если он соответствует тексту из XML
            if (botAPI.Text == Translate("TextBox"))
            {
                botAPI.Text = "";
            }
        }
    }
}
