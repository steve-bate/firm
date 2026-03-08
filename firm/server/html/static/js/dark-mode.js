// Initialize based on system preference
let prefersDarkScheme = window.matchMedia("(prefers-color-scheme: dark)").matches;
console.log('prefersDarkScheme (system)', prefersDarkScheme);

const storedMode = localStorage.getItem('dark-mode')
console.log('storedMode', storedMode);
if (storedMode !== null) {
    prefersDarkScheme = storedMode === 'true';
}
console.log('prefersDarkScheme (computed)', prefersDarkScheme);

if (prefersDarkScheme) {
    document.body.classList.add("dark-mode");
} else {
    document.body.classList.add("light-mode");
}

// Toggle dark mode manually
document.querySelector('.dark-mode-toggle').addEventListener('click', function () {
    if (document.body.classList.contains("dark-mode")) {
        document.body.classList.remove("dark-mode");
        document.body.classList.add("light-mode");
        localStorage.setItem('dark-mode', 'false');
    } else {
        document.body.classList.remove("light-mode");
        document.body.classList.add("dark-mode");
        localStorage.setItem('dark-mode', 'true');
    }
});
