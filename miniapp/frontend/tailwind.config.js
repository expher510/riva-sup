/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        background: '#0b0f19',
        surface: '#111827',
        card: '#161e2e',
        'card-border': '#1e293b',
        primary: {
          DEFAULT: '#6366f1',
          hover: '#4f46e5',
          dark: '#4338ca'
        },
        accent: '#8b5cf6',
        gold: '#f59e0b',
        cyan: '#06b6d4',
        red: '#ef4444'
      },
      fontFamily: {
        sans: ['Cairo', 'Tajawal', 'system-ui', 'sans-serif'],
      }
    },
  },
  plugins: [],
}

