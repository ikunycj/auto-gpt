import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import LoginPage from './pages/LoginPage';
import './styles.css';

const Root = window.location.pathname === '/login' ? LoginPage : App;
createRoot(document.getElementById('root')).render(
  <React.StrictMode><Root /></React.StrictMode>,
);
