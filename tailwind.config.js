/** @type {import('tailwindcss').Config} */
export default {
  content: ["./frontend/index.html", "./frontend/src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#F8F9FB",
        surface: "#FFFFFF",
        line: "#E9ECEF",
        "line-subtle": "#F1F3F5",
        "line-strong": "#CED4DA",
        ink: "#212529",
        "ink-2": "#495057",
        "ink-3": "#868E96",
        accent: "#3B5BDB",
        "accent-hover": "#364FC7",
        "accent-press": "#2B3E99",
        "accent-tint": "#EDF2FF",
        "red-bg": "#FFF5F5",
        "red-line": "#FFC9C9",
        "red-text": "#E03131",
        "amber-bg": "#FFF9DB",
        "amber-line": "#FFE066",
        "amber-text": "#9A6700",
        "green-bg": "#EBFBEE",
        "green-line": "#B2F2BB",
        "green-text": "#2B8A3E",
      },
      borderRadius: {
        DEFAULT: "0.25rem",
        lg: "0.5rem",
        xl: "0.75rem",
      },
      fontFamily: {
        sans: ["Inter", "Noto Sans SC", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Consolas", "monospace"],
      },
      boxShadow: {
        card: "0 1px 2px 0 rgba(16,24,40,0.04), 0 1px 3px 0 rgba(16,24,40,0.02)",
        pop: "0 4px 6px -1px rgba(16,24,40,0.06), 0 2px 4px -2px rgba(16,24,40,0.04)",
        modal: "0 20px 25px -5px rgba(16,24,40,0.10), 0 8px 10px -6px rgba(16,24,40,0.04)",
      },
    },
  },
  plugins: [],
}
