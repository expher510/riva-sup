import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import DetailsView from '../views/DetailsView.vue'
import RequestsView from '../views/RequestsView.vue'
import ProfileView from '../views/ProfileView.vue'
import CatalogListView from '../views/CatalogListView.vue'

const routes = [
  { path: '/', name: 'home', component: HomeView },
  { path: '/movies', name: 'movies', component: CatalogListView, props: { type: 'movie' } },
  { path: '/series', name: 'series', component: CatalogListView, props: { type: 'series' } },
  { path: '/details/:type/:id', name: 'details', component: DetailsView },
  { path: '/requests', name: 'requests', component: RequestsView },
  { path: '/profile', name: 'profile', component: ProfileView }
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    return { top: 0 }
  }
})

