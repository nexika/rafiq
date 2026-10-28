// The widget's own words, in Rafiq's six languages (like PHRASES in rafiq/lang.py). Egyptian Arabic is
// written as an Egyptian would say it. The Rust side sends problems as codes, and the page says them here.

export const TAGS = ["en", "fr", "es", "de", "ar", "ar-EG"];

export const PHRASES = {
  connecting: { en: "Connecting…", fr: "Connexion…", es: "Conectando…", de: "Verbinde…", ar: "جارٍ الاتصال…",
    "ar-EG": "بيوصل…" },
  not_running: { en: "Rafiq isn't running", fr: "Rafiq n'est pas lancé", es: "Rafiq no está en marcha",
    de: "Rafiq läuft nicht", ar: "رفيق لا يعمل الآن", "ar-EG": "رفيق مش شغال" },
  no_token: { en: "Start Rafiq once (rafiq run)", fr: "Lancez Rafiq une fois (rafiq run)",
    es: "Inicia Rafiq una vez (rafiq run)", de: "Starte Rafiq einmal (rafiq run)",
    ar: "شغّل رفيق مرة أولًا (rafiq run)", "ar-EG": "شغّل رفيق مرة الأول (rafiq run)" },
  bad_token: { en: "Rafiq's token file looks wrong", fr: "Le fichier de jeton de Rafiq semble incorrect",
    es: "El archivo de token de Rafiq parece incorrecto", de: "Rafiqs Token-Datei sieht falsch aus",
    ar: "ملف رمز رفيق غير صحيح", "ar-EG": "ملف التوكن بتاع رفيق شكله غلط" },
  bad_config: { en: "config.toml can't be read", fr: "config.toml est illisible", es: "No se puede leer config.toml",
    de: "config.toml ist nicht lesbar", ar: "تعذّرت قراءة config.toml", "ar-EG": "مش قادر أقرا config.toml" },
  other_host: { en: "The panel isn't on this computer", fr: "Le panneau n'est pas sur cet ordinateur",
    es: "El panel no está en este ordenador", de: "Das Panel ist nicht auf diesem Computer",
    ar: "اللوحة ليست على هذا الحاسوب", "ar-EG": "اللوحة مش على الكمبيوتر ده" },
  not_ours: { en: "Another program holds Rafiq's port", fr: "Un autre programme occupe le port de Rafiq",
    es: "Otro programa ocupa el puerto de Rafiq", de: "Ein anderes Programm belegt Rafiqs Port",
    ar: "برنامج آخر يشغل منفذ رفيق", "ar-EG": "فيه برنامج تاني ماسك البورت بتاع رفيق" },
  refused: { en: "Rafiq refused the widget", fr: "Rafiq a refusé le widget", es: "Rafiq rechazó el widget",
    de: "Rafiq hat das Widget abgelehnt", ar: "رفض رفيق الأداة", "ar-EG": "رفيق رفض الويدجت" },
  stopped: { en: "Rafiq stopped", fr: "Rafiq s'est arrêté", es: "Rafiq se detuvo", de: "Rafiq wurde beendet",
    ar: "توقف رفيق", "ar-EG": "رفيق وقف" },
  state_idle: { en: "Ready", fr: "Prêt", es: "Listo", de: "Bereit", ar: "جاهز", "ar-EG": "جاهز" },
  state_listening: { en: "Listening…", fr: "J'écoute…", es: "Escuchando…", de: "Ich höre zu…", ar: "أستمع…",
    "ar-EG": "بسمعك…" },
  state_thinking: { en: "Thinking…", fr: "Je réfléchis…", es: "Pensando…", de: "Ich denke nach…", ar: "أفكر…",
    "ar-EG": "بفكر…" },
  state_speaking: { en: "Speaking", fr: "Je parle", es: "Hablando", de: "Ich spreche", ar: "أتحدث", "ar-EG": "بتكلم" },
  state_dictating: { en: "Dictating", fr: "Dictée", es: "Dictando", de: "Diktat", ar: "إملاء",
    "ar-EG": "بكتب اللي بتقوله" },
  state_calibrating: { en: "Listening to the room", fr: "J'écoute la pièce", es: "Escuchando la sala",
    de: "Ich höre den Raum ab", ar: "أستمع إلى الغرفة", "ar-EG": "بسمع الأوضة" },
  show_more: { en: "Show more", fr: "Afficher plus", es: "Mostrar más", de: "Mehr zeigen", ar: "عرض المزيد",
    "ar-EG": "وريني أكتر" },
  show_less: { en: "Show less", fr: "Afficher moins", es: "Mostrar menos", de: "Weniger zeigen", ar: "عرض أقل",
    "ar-EG": "صغّر" },
  card_music: { en: "Music", fr: "Musique", es: "Música", de: "Musik", ar: "موسيقى", "ar-EG": "مزيكا" },
  card_news: { en: "News", fr: "Actualités", es: "Noticias", de: "Nachrichten", ar: "أخبار", "ar-EG": "أخبار" },
  card_weather: { en: "Weather", fr: "Météo", es: "Tiempo", de: "Wetter", ar: "الطقس", "ar-EG": "الجو" },
  card_timer: { en: "Timer", fr: "Minuteur", es: "Temporizador", de: "Timer", ar: "مؤقت", "ar-EG": "تايمر" },
  rafiq_explains: { en: "Rafiq explains", fr: "Rafiq explique", es: "Rafiq explica", de: "Rafiq erklärt",
    ar: "رفيق يشرح", "ar-EG": "رفيق بيشرح" },
  from_program: { en: "Another program, through Rafiq", fr: "Un autre programme, via Rafiq",
    es: "Otro programa, a través de Rafiq", de: "Ein anderes Programm, über Rafiq", ar: "برنامج آخر عبر رفيق",
    "ar-EG": "برنامج تاني عن طريق رفيق" },
};

/** The phrase in this language, or in English; null for a key there is no phrase for. */
export function t(key, tag) {
  const table = PHRASES[key];
  return table ? table[tag] || table.en : null;
}
