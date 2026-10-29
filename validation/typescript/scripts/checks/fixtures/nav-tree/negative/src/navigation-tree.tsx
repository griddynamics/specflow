import { Package, BookOpen } from 'lucide-react';

const NAV_ITEMS = [
  {
    kind: 'link',
    labelKey: 'nav.orders',
    to: '/orders',
    icon: Package, // wrong icon: declared ShoppingCart
  },
  {
    kind: 'group',
    labelKey: 'nav.catalog',
    icon: BookOpen,
    children: [
      // missing the declared "Products" (/catalog/products) child entirely
    ],
  },
];
