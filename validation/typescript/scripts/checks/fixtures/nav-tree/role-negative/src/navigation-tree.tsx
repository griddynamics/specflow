import { ShoppingCart, BookOpen, Tag, Settings, ClipboardList, Users } from 'lucide-react';

const NAV_ITEMS = [
  {
    // Unrestricted route wearing a restriction: excess, own flag.
    kind: 'link',
    labelKey: 'nav.orders',
    to: '/orders',
    icon: ShoppingCart,
    adminOnly: true,
  },
  {
    kind: 'group',
    labelKey: 'nav.catalog',
    icon: BookOpen,
    children: [
      {
        kind: 'link',
        labelKey: 'nav.catalog_products',
        to: '/catalog/products',
        icon: Tag,
      },
      {
        // Restricted route with no restriction at all: missing.
        kind: 'link',
        labelKey: 'nav.catalog_suppliers',
        to: '/catalog/suppliers',
        icon: BookOpen,
      },
    ],
  },
  {
    kind: 'group',
    labelKey: 'nav.administration',
    icon: Settings,
    adminOnly: true,
    children: [
      {
        // Unrestricted route hidden by the group's restriction: excess, inherited.
        kind: 'link',
        labelKey: 'nav.activity_log',
        to: '/activity',
        icon: ClipboardList,
      },
      {
        // Restricted route, restriction inherited from the group: correct.
        kind: 'link',
        labelKey: 'nav.users',
        to: '/administration/users',
        icon: Users,
      },
    ],
  },
];
