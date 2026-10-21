import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import App from './App';
import LoginPage from './pages/LoginPage';
import './styles.css';
import './workspace.css';

function Root() {
  if (window.location.pathname === '/login') return <LoginPage />;
  return <BrowserRouter><App /></BrowserRouter>;
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode><Root /></React.StrictMode>,
);
